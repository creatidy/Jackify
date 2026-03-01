"""
Wabbajack Installer Handler

Automated Wabbajack.exe installation and configuration via Proton.

Provides: Wabbajack.exe download, Steam shortcuts.vdf handling,
WebView2 install, Windows version registry overrides, optional Heroic GOG detection.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.request
import zlib
from pathlib import Path
from typing import Optional, List, Dict, Tuple

try:
    import vdf
except ImportError:
    vdf = None


class WabbajackInstallerHandler:
    """Handles automated Wabbajack installation via Proton"""

    # Download URLs
    WABBAJACK_URL = "https://github.com/wabbajack-tools/wabbajack/releases/latest/download/Wabbajack.exe"
    WEBVIEW2_URLS = (
        "https://files.omnigaming.org/MicrosoftEdgeWebView2RuntimeInstallerX64-WabbajackProton.exe",
        "https://go.microsoft.com/fwlink/p/?LinkId=2124703",
        "https://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/6d376ab4-4a07-4679-8918-e0dc3c0735c8/MicrosoftEdgeWebView2RuntimeInstallerX64.exe",
    )

    # Minimal Win10 registry settings for WebView2 compatibility
    WIN10_REGISTRY = """REGEDIT4

[HKEY_LOCAL_MACHINE\\Software\\Microsoft\\Windows NT\\CurrentVersion]
"ProductName"="Microsoft Windows 10"
"CSDVersion"=""
"CurrentBuild"="19045"
"CurrentBuildNumber"="19045"
"CurrentVersion"="10.0"

[HKEY_LOCAL_MACHINE\\System\\CurrentControlSet\\Control\\Windows]
"CSDVersion"=dword:00000000

[HKEY_CURRENT_USER\\Software\\Wine\\AppDefaults\\Wabbajack.exe\\X11 Driver]
"Decorated"="N"
"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    def calculate_app_id(self, exe_path: str, app_name: str) -> int:
        """
        Calculate Steam AppID using CRC32 algorithm.

        Args:
            exe_path: Path to executable (must be quoted)
            app_name: Application name

        Returns:
            AppID (31-bit to fit signed 32-bit integer range for VDF binary format)
        """
        input_str = f"{exe_path}{app_name}"
        crc = zlib.crc32(input_str.encode()) & 0x7FFFFFFF  # Use 31 bits for signed int
        return crc

    def find_steam_userdata_path(self) -> Optional[Path]:
        """
        Find most recently used Steam userdata directory.

        Returns:
            Path to userdata/<userid> or None if not found
        """
        home = Path.home()
        steam_paths = [
            home / ".steam/steam",
            home / ".local/share/Steam",
            home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
        ]

        for steam_path in steam_paths:
            userdata = steam_path / "userdata"
            if not userdata.exists():
                continue

            # Find most recently modified numeric user directory
            user_dirs = []
            for entry in userdata.iterdir():
                if entry.is_dir() and entry.name.isdigit():
                    user_dirs.append(entry)

            if user_dirs:
                # Sort by modification time (most recent first)
                user_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                self.logger.info(f"Found Steam userdata: {user_dirs[0]}")
                return user_dirs[0]

        return None

    def get_shortcuts_vdf_path(self) -> Optional[Path]:
        """Get path to shortcuts.vdf file"""
        userdata = self.find_steam_userdata_path()
        if userdata:
            return userdata / "config/shortcuts.vdf"
        return None

    def add_to_steam_shortcuts(self, exe_path: Path) -> int:
        """
        Add Wabbajack to Steam shortcuts.vdf and return calculated AppID.

        Args:
            exe_path: Path to Wabbajack.exe

        Returns:
            Calculated AppID

        Raises:
            RuntimeError: If vdf library not available or shortcuts.vdf not found
        """
        if vdf is None:
            raise RuntimeError("vdf library not installed. Install with: pip install vdf")

        shortcuts_path = self.get_shortcuts_vdf_path()
        if not shortcuts_path:
            raise RuntimeError("Could not find Steam shortcuts.vdf path")

        self.logger.info(f"Shortcuts.vdf path: {shortcuts_path}")

        # Read existing shortcuts or create new
        if shortcuts_path.exists():
            with open(shortcuts_path, 'rb') as f:
                shortcuts = vdf.binary_load(f)
        else:
            shortcuts = {'shortcuts': {}}
            # Ensure parent directory exists
            shortcuts_path.parent.mkdir(parents=True, exist_ok=True)

        # Calculate AppID
        exe_str = f'"{str(exe_path)}"'
        app_id = self.calculate_app_id(exe_str, "Wabbajack")

        self.logger.info(f"Calculated AppID: {app_id}")

        # Create shortcut entry
        idx = str(len(shortcuts.get('shortcuts', {})))
        shortcuts.setdefault('shortcuts', {})[idx] = {
            'appid': app_id,
            'AppName': 'Wabbajack',
            'Exe': exe_str,
            'StartDir': f'"{str(exe_path.parent)}"',
            'icon': str(exe_path),
            'ShortcutPath': '',
            'LaunchOptions': '',
            'IsHidden': 0,
            'AllowDesktopConfig': 1,
            'AllowOverlay': 1,
            'OpenVR': 0,
            'Devkit': 0,
            'DevkitGameID': '',
            'DevkitOverrideAppID': 0,
            'LastPlayTime': 0,
            'FlatpakAppID': '',
            'tags': {}
        }

        # Write back (binary format)
        with open(shortcuts_path, 'wb') as f:
            vdf.binary_dump(shortcuts, f)

        self.logger.info(f"Added Wabbajack to Steam shortcuts with AppID {app_id}")
        return app_id

    def create_dotnet_cache(self, install_folder: Path):
        """
        Create .NET bundle extract cache directory.

        Wabbajack requires: <install_path>/<home_path>/.cache/dotnet_bundle_extract

        Args:
            install_folder: Wabbajack installation directory
        """
        home = Path.home()
        # Strip leading slash to make it relative
        home_relative = str(home).lstrip('/')
        cache_dir = install_folder / home_relative / '.cache/dotnet_bundle_extract'

        cache_dir.mkdir(parents=True, exist_ok=True)
        self.logger.info(f"Created dotnet cache: {cache_dir}")

    def download_file(self, url: str, dest: Path, description: str = "file") -> None:
        """
        Download file with progress logging.

        Args:
            url: Download URL
            dest: Destination path
            description: Description for logging

        Raises:
            RuntimeError: If download fails
        """
        self.logger.info(f"Downloading {description} from {url}")

        try:
            # Ensure parent directory exists
            dest.parent.mkdir(parents=True, exist_ok=True)

            # Download with user agent
            request = urllib.request.Request(
                url,
                headers={'User-Agent': 'Jackify-WabbajackInstaller'}
            )

            with urllib.request.urlopen(request, timeout=60) as response:
                with open(dest, 'wb') as f:
                    shutil.copyfileobj(response, f)

            self.logger.info(f"Downloaded {description} to {dest}")

        except Exception as e:
            raise RuntimeError(f"Failed to download {description}: {e}")

    def _is_valid_windows_executable(self, file_path: Path) -> bool:
        """
        Basic validation for downloaded Windows installer files.

        Checks for:
        - minimum plausible size (>100 KB)
        - PE header signature ("MZ")
        """
        if not file_path.exists() or file_path.stat().st_size < 100 * 1024:
            return False

        try:
            with open(file_path, 'rb') as f:
                return f.read(2) == b'MZ'
        except Exception:
            return False

    def download_webview2_installer(self, dest: Path) -> None:
        """
        Download WebView2 installer using fallback URLs.

        Raises:
            RuntimeError: If all download attempts fail or downloaded file is invalid
        """
        errors = []

        for url in self.WEBVIEW2_URLS:
            try:
                self.download_file(url, dest, "WebView2 installer")
                if self._is_valid_windows_executable(dest):
                    self.logger.info(f"Downloaded valid WebView2 installer from {url}")
                    return

                errors.append(f"{url} -> downloaded file is not a valid Windows executable")
                self.logger.warning(f"Invalid WebView2 installer downloaded from {url}, trying next source...")
                try:
                    dest.unlink(missing_ok=True)
                except Exception:
                    pass

            except RuntimeError as e:
                errors.append(f"{url} -> {e}")
                self.logger.warning(f"Failed to download WebView2 installer from {url}: {e}")

        raise RuntimeError(
            "Failed to download a valid WebView2 installer from all sources.\n"
            + "\n".join(errors)
        )

    def download_wabbajack(self, install_folder: Path) -> Path:
        """
        Download Wabbajack.exe to installation folder.

        Args:
            install_folder: Installation directory

        Returns:
            Path to downloaded Wabbajack.exe
        """
        install_folder.mkdir(parents=True, exist_ok=True)
        wabbajack_exe = install_folder / "Wabbajack.exe"

        # Skip if already exists
        if wabbajack_exe.exists():
            self.logger.info(f"Wabbajack.exe already exists at {wabbajack_exe}")
            return wabbajack_exe

        self.download_file(self.WABBAJACK_URL, wabbajack_exe, "Wabbajack.exe")
        return wabbajack_exe

    def find_proton_experimental(self) -> Optional[Path]:
        """
        Find Proton Experimental installation path.

        Returns:
            Path to Proton Experimental directory or None
        """
        home = Path.home()
        steam_paths = [
            home / ".steam/steam",
            home / ".local/share/Steam",
            home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
        ]

        for steam_path in steam_paths:
            proton_path = steam_path / "steamapps/common/Proton - Experimental"
            if proton_path.exists():
                self.logger.info(f"Found Proton Experimental: {proton_path}")
                return proton_path

        return None

    def get_compat_data_path(self, app_id: int) -> Optional[Path]:
        """
        Get compatdata path for AppID. Uses same detection logic as create_prefix_with_proton_wrapper.

        Priority:
        1. Check if prefix already exists at any known location
        2. Use PathHandler library detection (Flatpak-aware via libraryfolders.vdf)
        3. Fallback to native ~/.steam/steam
        """
        from .path_handler import PathHandler
        path_handler = PathHandler()
        all_libraries = path_handler.get_all_steam_library_paths()

        # Check if Flatpak Steam by looking for .var/app/com.valvesoftware.Steam in library paths
        is_flatpak_steam = any('.var/app/com.valvesoftware.Steam' in str(lib) for lib in all_libraries)

        # Determine compatdata root using same logic as create_prefix_with_proton_wrapper
        if is_flatpak_steam and all_libraries:
            # Flatpak Steam: use first library root (from libraryfolders.vdf)
            library_root = all_libraries[0]
            compatdata_dir = library_root / "steamapps/compatdata"
            self.logger.debug(f"Flatpak Steam detected, using library root: {library_root}")
        else:
            # Native Steam
            compatdata_dir = Path.home() / ".steam/steam/steamapps/compatdata"
            self.logger.debug("Native Steam detected")

        compat_path = compatdata_dir / str(app_id)

        # Check if prefix already exists there
        if compat_path.exists():
            self.logger.debug(f"Found existing compatdata at: {compat_path}")
            return compat_path

        # Prefix doesn't exist yet - return expected path if compatdata root exists
        if compatdata_dir.is_dir():
            self.logger.debug(f"Using compatdata location: {compat_path}")
            return compat_path

        self.logger.warning(f"Compatdata root does not exist: {compatdata_dir}")
        return None

    def init_wine_prefix(self, app_id: int, proton_path: Optional[Path] = None) -> Path:
        """
        Initialize Wine prefix using Proton.

        Args:
            app_id: Steam AppID
            proton_path: Optional path to Proton directory; if None, uses Proton Experimental

        Returns:
            Path to created prefix

        Raises:
            RuntimeError: If prefix creation fails
        """
        proton_path = proton_path or self.find_proton_experimental()
        if not proton_path:
            raise RuntimeError("Proton not found. Install a Proton version in Steam or set Install Proton in Settings.")

        compat_data = self.get_compat_data_path(app_id)
        if not compat_data:
            raise RuntimeError("Could not determine compatdata path")

        prefix_path = compat_data / "pfx"

        # Create compat data directory
        compat_data.mkdir(parents=True, exist_ok=True)

        # Run wineboot to initialize prefix
        proton_bin = proton_path / "proton"
        env = os.environ.copy()
        env['STEAM_COMPAT_DATA_PATH'] = str(compat_data)
        env['STEAM_COMPAT_CLIENT_INSTALL_PATH'] = str(compat_data.parent.parent.parent)
        # Suppress GUI windows
        env['DISPLAY'] = ''
        env['WAYLAND_DISPLAY'] = ''
        env['WINEDEBUG'] = '-all'
        env['WINEDLLOVERRIDES'] = 'msdia80.dll=n;conhost.exe=d;cmd.exe=d'

        self.logger.info(f"Initializing Wine prefix for AppID {app_id}...")
        result = subprocess.run(
            [str(proton_bin), 'run', 'wineboot', '-u'],
            env=env,
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode != 0:
            raise RuntimeError(f"Failed to initialize Wine prefix: {result.stderr}")

        self.logger.info(f"Prefix created: {prefix_path}")
        return prefix_path

    def run_in_prefix(self, app_id: int, exe_path: Path, args: List[str] = None, proton_path: Optional[Path] = None) -> None:
        """
        Run executable in Wine prefix using Proton.

        Args:
            app_id: Steam AppID
            exe_path: Path to executable
            args: Optional command line arguments
            proton_path: Optional path to Proton directory; if None, uses Proton Experimental

        Raises:
            RuntimeError: If execution fails
        """
        proton_path = proton_path or self.find_proton_experimental()
        if not proton_path:
            raise RuntimeError("Proton not found")

        compat_data = self.get_compat_data_path(app_id)
        if not compat_data:
            raise RuntimeError("Could not determine compatdata path")

        proton_bin = proton_path / "proton"
        cmd = [str(proton_bin), 'run', str(exe_path)]
        if args:
            cmd.extend(args)

        env = os.environ.copy()
        env['STEAM_COMPAT_DATA_PATH'] = str(compat_data)
        env['STEAM_COMPAT_CLIENT_INSTALL_PATH'] = str(compat_data.parent.parent.parent)
        # Suppress Wine debug output
        env['WINEDEBUG'] = '-all'
        # Suppress cmd.exe and conhost.exe windows (the flickers you see)
        # Keep DISPLAY so installers can run, but prevent console windows
        env['WINEDLLOVERRIDES'] = 'msdia80.dll=n;conhost.exe=d;cmd.exe=d'

        self.logger.info(f"Running {exe_path.name} in prefix...")
        self.logger.debug(f"Command: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode != 0:
            error_msg = f"Failed to run {exe_path.name} (exit code {result.returncode})"
            if result.stderr:
                error_msg += f"\nStderr: {result.stderr}"
            if result.stdout:
                error_msg += f"\nStdout: {result.stdout}"
            self.logger.error(error_msg)
            self.logger.debug(f"Full command output - returncode: {result.returncode}, stdout length: {len(result.stdout) if result.stdout else 0}, stderr length: {len(result.stderr) if result.stderr else 0}")
            raise RuntimeError(error_msg)

    def apply_registry(self, app_id: int, reg_content: str, proton_path: Optional[Path] = None) -> None:
        """
        Apply registry content to Wine prefix.

        Args:
            app_id: Steam AppID
            reg_content: Registry file content
            proton_path: Optional path to Proton directory; if None, uses Proton Experimental

        Raises:
            RuntimeError: If registry application fails
        """
        proton_path = proton_path or self.find_proton_experimental()
        if not proton_path:
            raise RuntimeError("Proton not found")

        compat_data = self.get_compat_data_path(app_id)
        if not compat_data:
            raise RuntimeError("Could not determine compatdata path")

        prefix_path = compat_data / "pfx"
        if not prefix_path.exists():
            raise RuntimeError(f"Prefix not found: {prefix_path}")

        # Write registry content to temp file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.reg', delete=False) as f:
            f.write(reg_content)
            temp_reg = Path(f.name)

        try:
            # Use Proton's wine directly
            wine_bin = proton_path / "files/bin/wine64"

            self.logger.info("Applying registry settings...")
            env = os.environ.copy()
            env['WINEPREFIX'] = str(prefix_path)
            result = subprocess.run(
                [str(wine_bin), 'regedit', str(temp_reg)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                raise RuntimeError(f"Failed to apply registry: {result.stderr}")

            self.logger.info("Registry settings applied")

        finally:
            # Cleanup temp file
            if temp_reg.exists():
                temp_reg.unlink()

    def install_webview2(self, app_id: int, install_folder: Path, proton_path: Optional[Path] = None) -> None:
        """
        Download and install WebView2 runtime.

        Args:
            app_id: Steam AppID
            install_folder: Directory to download installer to
            proton_path: Optional path to Proton directory; if None, uses Proton Experimental

        Raises:
            RuntimeError: If installation fails
        """
        webview_installer = install_folder / "webview2_installer.exe"

        # Download installer with fallback URLs
        self.download_webview2_installer(webview_installer)

        try:
            # Run installer with silent flags
            self.logger.info("Installing WebView2 (this may take a minute)...")
            self.logger.info(f"WebView2 installer path: {webview_installer}")
            self.logger.info(f"AppID: {app_id}")
            try:
                self.run_in_prefix(app_id, webview_installer, ["/silent", "/install"], proton_path=proton_path)
                self.logger.info("WebView2 installed successfully")
            except RuntimeError as e:
                error_str = str(e)
                # Exit code 8 might mean "already installed" - log but don't fail
                if "exit code 8" in error_str:
                    self.logger.warning(f"WebView2 installer returned exit code 8: {error_str}")
                    self.logger.warning("This may indicate WebView2 is already installed. Continuing...")
                    # Don't raise - treat as non-fatal
                    return
                self.logger.error(f"WebView2 installation failed: {e}")
                # Re-raise for other errors
                raise

        finally:
            # Cleanup installer
            if webview_installer.exists():
                try:
                    webview_installer.unlink()
                    self.logger.debug("Cleaned up WebView2 installer")
                except Exception as e:
                    self.logger.warning(f"Failed to cleanup WebView2 installer: {e}")

    def apply_win10_registry(self, app_id: int, proton_path: Optional[Path] = None) -> None:
        """
        Apply Windows 10 registry settings.

        Args:
            app_id: Steam AppID
            proton_path: Optional path to Proton directory; if None, uses Proton Experimental

        Raises:
            RuntimeError: If registry application fails
        """
        self.apply_registry(app_id, self.WIN10_REGISTRY, proton_path=proton_path)

    def detect_heroic_gog_games(self) -> List[Dict]:
        """
        Detect GOG games installed via Heroic Games Launcher.

        Returns:
            List of dicts with keys: app_name, title, install_path, build_id
        """
        heroic_paths = [
            Path.home() / ".config/heroic",
            Path.home() / ".var/app/com.heroicgameslauncher.hgl/config/heroic"
        ]

        for heroic_path in heroic_paths:
            if not heroic_path.exists():
                continue

            installed_json = heroic_path / "gog_store/installed.json"
            if not installed_json.exists():
                continue

            try:
                # Read installed games
                with open(installed_json) as f:
                    data = json.load(f)
                    installed = data.get('installed', [])

                # Read library for titles
                library_json = heroic_path / "store_cache/gog_library.json"
                titles = {}
                if library_json.exists():
                    with open(library_json) as f:
                        lib = json.load(f)
                        titles = {g['app_name']: g['title'] for g in lib.get('games', [])}

                # Build game list
                games = []
                for game in installed:
                    app_name = game.get('appName')
                    if not app_name:
                        continue

                    games.append({
                        'app_name': app_name,
                        'title': titles.get(app_name, f"GOG Game {app_name}"),
                        'install_path': game.get('install_path', ''),
                        'build_id': game.get('buildId', '')
                    })

                if games:
                    self.logger.info(f"Found {len(games)} GOG games from Heroic")
                    for game in games:
                        self.logger.debug(f"  - {game['title']} ({game['app_name']})")

                return games

            except Exception as e:
                self.logger.warning(f"Failed to read Heroic config: {e}")
                continue

        return []

    def generate_gog_registry(self, games: List[Dict]) -> str:
        """
        Generate registry file content for GOG games.

        Args:
            games: List of GOG game dicts from detect_heroic_gog_games()

        Returns:
            Registry file content
        """
        reg = "REGEDIT4\n\n"
        reg += "[HKEY_LOCAL_MACHINE\\Software\\GOG.com]\n\n"
        reg += "[HKEY_LOCAL_MACHINE\\Software\\GOG.com\\Games]\n\n"
        reg += "[HKEY_LOCAL_MACHINE\\Software\\WOW6432Node\\GOG.com]\n\n"
        reg += "[HKEY_LOCAL_MACHINE\\Software\\WOW6432Node\\GOG.com\\Games]\n\n"

        for game in games:
            # Convert Linux path to Wine Z: drive
            linux_path = game['install_path']
            wine_path = f"Z:{linux_path}".replace('/', '\\\\')

            # Add to both 32-bit and 64-bit registry locations
            for prefix in ['Software\\GOG.com\\Games', 'Software\\WOW6432Node\\GOG.com\\Games']:
                reg += f"[HKEY_LOCAL_MACHINE\\{prefix}\\{game['app_name']}]\n"
                reg += f'"path"="{wine_path}"\n'
                reg += f'"gameID"="{game["app_name"]}"\n'
                reg += f'"gameName"="{game["title"]}"\n'
                reg += f'"buildId"="{game["build_id"]}"\n'
                reg += f'"workingDir"="{wine_path}"\n\n'

        return reg

    def inject_gog_registry(self, app_id: int) -> int:
        """
        Inject Heroic GOG games into Wine prefix registry.

        Args:
            app_id: Steam AppID

        Returns:
            Number of games injected
        """
        games = self.detect_heroic_gog_games()

        if not games:
            self.logger.info("No GOG games found in Heroic")
            return 0

        reg_content = self.generate_gog_registry(games)

        self.logger.info(f"Injecting {len(games)} GOG games into prefix...")
        self.apply_registry(app_id, reg_content)
        self.logger.info(f"Injected {len(games)} GOG games")
        return len(games)
