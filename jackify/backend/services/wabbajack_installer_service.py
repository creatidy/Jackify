"""
Wabbajack Installer Service

Backend service for orchestrating complete Wabbajack installation workflow.
Handles all 12 steps including Steam shortcuts, prefix creation, and configuration.
"""

import logging
import subprocess
import time
from pathlib import Path
from typing import Optional, Callable, Tuple

from ..handlers.wabbajack_installer_handler import WabbajackInstallerHandler
from ..handlers.config_handler import ConfigHandler
from ..handlers.wine_utils import WineUtils
from .native_steam_service import NativeSteamService
from .steam_restart_service import (
    start_steam, is_flatpak_steam, is_steam_deck, _get_clean_subprocess_env, robust_steam_restart,
    ensure_flatpak_steam_filesystem_access,
)
from .automated_prefix_service import AutomatedPrefixService

logger = logging.getLogger(__name__)


class WabbajackInstallerService:
    """Service for orchestrating Wabbajack installation workflow"""

    def __init__(self):
        self.handler = WabbajackInstallerHandler()
        self.steam_service = NativeSteamService()
        self.config_handler = ConfigHandler()
        self.prefix_service = AutomatedPrefixService()

    def _resolve_proton_path_and_name(self) -> Tuple[Optional[Path], Optional[str]]:
        """Resolve user's Install Proton path and Steam compat name. Fallback to Proton Experimental."""
        user_path = self.config_handler.get_proton_path()
        if user_path and user_path != 'auto':
            path = Path(user_path).expanduser()
            if path.is_dir():
                compat_name = WineUtils.resolve_steam_compat_name(path)
                if compat_name:
                    return path, compat_name
                dir_name = path.name
                if dir_name.startswith('GE-Proton'):
                    return path, dir_name
                steam_name = dir_name.lower().replace(' - ', '_').replace(' ', '_').replace('-', '_')
                if not steam_name.startswith('proton'):
                    steam_name = f"proton_{steam_name}"
                return path, steam_name
        best = WineUtils.select_best_proton()
        if best:
            return Path(best['path']), best['steam_compat_name']
        valve = WineUtils.select_best_valve_proton()
        if valve:
            return Path(valve['path']), valve.get('steam_compat_name', 'proton_experimental')
        logger.error("No Proton version found")
        return None, None

    def install_wabbajack(
        self,
        install_folder: Path,
        shortcut_name: str = "Wabbajack",
        enable_gog: bool = True,
        progress_callback: Optional[Callable[[str, int], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> Tuple[bool, Optional[int], Optional[str], Optional[int], Optional[str], Optional[str]]:
        """
        Execute complete Wabbajack installation workflow.

        Args:
            install_folder: Directory to install Wabbajack.exe
            shortcut_name: Name for Steam shortcut
            enable_gog: Whether to detect and inject GOG games
            progress_callback: Optional callback(status, percentage)
            log_callback: Optional callback for log messages

        Returns:
            Tuple of (success, app_id, launch_options, gog_count, time_taken_str, error_message)
        """
        start_time = time.time()
        total_steps = 12
        app_id = None
        launch_options = ""
        gog_count = 0

        def update_progress(message: str, step: int, percentage: int = None):
            if progress_callback:
                if percentage is None:
                    percentage = int((step / total_steps) * 100)
                progress_callback(message, percentage)
            if log_callback:
                log_callback(message)
            else:
                # Only log directly if no callback (callback already logs)
                logger.info(message)

        # Detect Steam installation type once at the start for consistent use throughout
        _is_steam_deck = is_steam_deck()
        _is_flatpak = is_flatpak_steam()

        if _is_flatpak:
            ensure_flatpak_steam_filesystem_access(install_folder)

        try:
            # Step 1: Check requirements
            update_progress("Checking requirements...", 1, 5)
            proton_path, proton_compat_name = self._resolve_proton_path_and_name()
            if not proton_path:
                return False, None, None, None, None, "Proton not found. Install a Proton version in Steam or set Install Proton in Settings."
            update_progress(f"Using Proton: {proton_path.name}", 1, 5)

            userdata = self.handler.find_steam_userdata_path()
            if not userdata:
                return False, None, None, None, None, "Steam userdata not found. Please ensure Steam is installed and you're logged in."
            update_progress(f"Found Steam userdata: {userdata}", 1, 5)

            # Step 2: Download Wabbajack.exe
            update_progress("Downloading Wabbajack.exe...", 2, 15)
            wabbajack_exe = self.handler.download_wabbajack(install_folder)
            if not wabbajack_exe:
                return False, None, None, None, None, "Failed to download Wabbajack.exe"
            update_progress(f"Downloaded to: {wabbajack_exe}", 2, 15)

            # Step 3: Create dotnet cache
            update_progress("Creating .NET cache directory...", 3, 20)
            self.handler.create_dotnet_cache(install_folder)
            update_progress(".NET cache created", 3, 20)

            # Step 4: Stop Steam briefly (required to safely modify shortcuts.vdf)
            # We'll do a full restart after creating the shortcut
            update_progress("Stopping Steam to modify shortcuts...", 4, 25)
            try:
                shutdown_env = _get_clean_subprocess_env()

                if _is_steam_deck:
                    subprocess.run(['systemctl', '--user', 'stop', 'app-steam@autostart.service'],
                                   timeout=15, check=False, capture_output=True, env=shutdown_env)
                elif _is_flatpak:
                    subprocess.run(['flatpak', 'kill', 'com.valvesoftware.Steam'],
                                   timeout=15, check=False, capture_output=True, env=shutdown_env)

                subprocess.run(['pkill', 'steam'], timeout=15, check=False, capture_output=True, env=shutdown_env)
                time.sleep(2)

                check_result = subprocess.run(['pgrep', '-f', 'steamwebhelper'], capture_output=True, timeout=10, env=shutdown_env)
                if check_result.returncode == 0:
                    subprocess.run(['pkill', '-9', 'steam'], timeout=15, check=False, capture_output=True, env=shutdown_env)
                    time.sleep(2)

                update_progress("Steam stopped", 4, 25)
            except Exception as e:
                update_progress(f"Warning: Steam shutdown had issues: {e}. Proceeding anyway...", 4, 25)

            # Step 5: Create Steam shortcut using NativeSteamService
            update_progress("Adding Wabbajack to Steam shortcuts...", 5, 30)
            
            # Generate launch options with STEAM_COMPAT_MOUNTS
            launch_options = ""
            try:
                from ..handlers.path_handler import PathHandler
                path_handler = PathHandler()
                mount_paths = path_handler.get_steam_compat_mount_paths(install_dir=str(install_folder))
                if mount_paths:
                    launch_options = f'STEAM_COMPAT_MOUNTS="{":".join(mount_paths)}" %command%'
                    update_progress(f"Added STEAM_COMPAT_MOUNTS for Steam libraries: {mount_paths}", 5, 30)
                else:
                    update_progress("No additional Steam libraries found - using empty launch options", 5, 30)
            except Exception as e:
                update_progress(f"Could not generate STEAM_COMPAT_MOUNTS (non-critical): {e}", 5, 30)

            success, app_id = self.steam_service.create_shortcut_with_proton(
                app_name=shortcut_name,
                exe_path=str(wabbajack_exe),
                start_dir=str(wabbajack_exe.parent),
                launch_options=launch_options,
                tags=["Jackify"],
                proton_version=proton_compat_name
            )
            if not success or app_id is None:
                return False, None, None, None, None, "Failed to create Steam shortcut"
            update_progress(f"Created Steam shortcut with AppID: {app_id}", 5, 30)

            # Step 5b: Restart Steam (same pattern as modlist workflows)
            update_progress("Restarting Steam...", 5, 35)
            def restart_callback(msg):
                update_progress(msg, 5, 35)

            if not robust_steam_restart(progress_callback=restart_callback):
                update_progress("Warning: Steam restart had issues, continuing anyway...", 5, 35)
            else:
                update_progress("Steam restarted successfully", 5, 40)

            # Step 6: Initialize Wine prefix (using same method as modlist workflows)
            update_progress("Creating Proton prefix...", 6, 45)
            try:
                if self.prefix_service.create_prefix_with_proton_wrapper(app_id):
                    prefix_path = self.prefix_service.get_prefix_path(app_id)
                    update_progress(f"Proton prefix created: {prefix_path}", 6, 45)
                else:
                    update_progress("Warning: Prefix creation returned False, continuing anyway...", 6, 45)
            except Exception as e:
                update_progress(f"Warning: Failed to create prefix: {e}", 6, 45)
                update_progress("Continuing anyway...", 6, 45)

            # Step 7: Ensure Win10 registry before WebView2 install
            update_progress("Applying Windows 10 registry settings...", 7, 55)
            try:
                self.handler.apply_win10_registry(app_id, proton_path=proton_path)
                update_progress("Windows 10 registry settings applied", 7, 55)
            except Exception as e:
                update_progress(f"Warning: Failed to apply Windows 10 registry settings: {e}", 7, 55)
                update_progress("Continuing anyway...", 7, 55)

            # Step 8: Install WebView2
            update_progress("Installing WebView2 runtime...", 8, 60)
            try:
                self.handler.install_webview2(app_id, install_folder, proton_path=proton_path)
                update_progress("WebView2 installed successfully", 8, 60)
            except Exception as e:
                update_progress(f"WARNING: WebView2 installation may have failed: {e}", 8, 60)
                update_progress("This may prevent Nexus login in Wabbajack. You can manually install WebView2 later.", 8, 60)

            # Step 9: GOG game detection (optional)
            if enable_gog:
                update_progress("Detecting GOG games from Heroic...", 9, 80)
                try:
                    gog_count = self.handler.inject_gog_registry(app_id)
                    if gog_count > 0:
                        update_progress(f"Detected and injected {gog_count} GOG games", 9, 80)
                    else:
                        update_progress("No GOG games found in Heroic", 9, 80)
                except Exception as e:
                    update_progress(f"GOG injection failed (non-critical): {e}", 9, 80)
            else:
                update_progress("Skipping GOG game detection", 9, 80)

            # Step 10: Create Steam library symlinks
            update_progress("Creating Steam library symlinks...", 10, 85)
            try:
                self.steam_service.create_steam_library_symlinks(app_id)
                update_progress("Steam library symlinks created", 10, 85)
            except Exception as e:
                update_progress(f"Warning: Failed to create symlinks: {e}", 10, 85)

            # Step 11: Verify Proton compatibility (was set at shortcut creation)
            update_progress(f"Proton version: {proton_compat_name}", 11, 90)

            # Step 12: Verify Steam is running (was restarted after shortcut creation)
            update_progress("Verifying Steam is running...", 12, 95)
            check_result = subprocess.run(['pgrep', '-f', 'steamwebhelper'], capture_output=True, timeout=10)
            if check_result.returncode == 0:
                update_progress("Steam is running", 12, 95)
            else:
                update_progress("Starting Steam...", 12, 95)
                if start_steam(is_steamdeck_flag=_is_steam_deck, is_flatpak_flag=_is_flatpak):
                    update_progress("Steam started successfully", 12, 95)
                    time.sleep(3)
                else:
                    update_progress("Warning: Please start Steam manually", 12, 95)

            # Calculate time taken
            time_taken = int(time.time() - start_time)
            mins, secs = divmod(time_taken, 60)
            time_str = f"{mins} minutes, {secs} seconds" if mins else f"{secs} seconds"

            update_progress("Installation complete!", 12, 100)
            update_progress(f"Wabbajack installed to: {install_folder}", 12, 100)
            update_progress(f"Steam AppID: {app_id}", 12, 100)

            return True, app_id, launch_options, gog_count, time_str, None

        except Exception as e:
            error_msg = f"Installation failed: {str(e)}"
            logger.error(error_msg, exc_info=True)
            if log_callback:
                log_callback(f"ERROR: {error_msg}")
            return False, None, None, None, None, error_msg
