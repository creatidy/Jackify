"""CLI configuration phase methods for ModlistInstallCLI (Mixin)."""
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from ..handlers.ui_colors import (
    COLOR_PROMPT,
    COLOR_RESET,
    COLOR_INFO,
    COLOR_ERROR,
    COLOR_SUCCESS,
    COLOR_WARNING,
)
from ..utils.wabbajack_inline_repair import repair_local_wabbajack_if_needed

logger = logging.getLogger(__name__)


class ModlistOperationsConfigurationCLIMixin:
    """Mixin providing CLI configuration phase methods."""

    def configuration_phase(self):
        """
        Run the configuration phase: execute the Linux-native Jackify Install Engine.
        """
        from .modlist_operations import get_jackify_engine_path

        print(f"\n{COLOR_PROMPT}--- Configuration Phase: Installing Modlist ---{COLOR_RESET}")
        start_time = time.time()

        from jackify.shared.paths import get_jackify_logs_dir
        log_dir = get_jackify_logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        workflow_log_path = log_dir / "Modlist_Install_workflow.log"
        max_logs = 3
        max_size = 1024 * 1024
        if workflow_log_path.exists() and workflow_log_path.stat().st_size > max_size:
            for i in range(max_logs, 0, -1):
                prev = log_dir / f"Modlist_Install_workflow.log.{i-1}" if i > 1 else workflow_log_path
                dest = log_dir / f"Modlist_Install_workflow.log.{i}"
                if prev.exists():
                    if dest.exists():
                        dest.unlink()
                    prev.rename(dest)
        workflow_log = open(workflow_log_path, 'a')
        class TeeStdout:
            def __init__(self, *files):
                self.files = files
            def write(self, data):
                for f in self.files:
                    f.write(data)
                    f.flush()
            def flush(self):
                for f in self.files:
                    f.flush()
        orig_stdout, orig_stderr = sys.stdout, sys.stderr
        sys.stdout = TeeStdout(sys.stdout, workflow_log)
        sys.stderr = TeeStdout(sys.stderr, workflow_log)
        try:
            install_dir_context = self.context['install_dir']
            if isinstance(install_dir_context, tuple):
                actual_install_path = Path(install_dir_context[0])
                if install_dir_context[1]:
                    self.logger.info(f"Creating install directory as it was marked for creation: {actual_install_path}")
                    actual_install_path.mkdir(parents=True, exist_ok=True)
            else:
                actual_install_path = Path(install_dir_context)
            install_dir_str = str(actual_install_path)
            self.logger.debug(f"Processed install directory for engine: {install_dir_str}")

            download_dir_context = self.context['download_dir']
            if isinstance(download_dir_context, tuple):
                actual_download_path = Path(download_dir_context[0])
                if download_dir_context[1]:
                    self.logger.info(f"Creating download directory as it was marked for creation: {actual_download_path}")
                    actual_download_path.mkdir(parents=True, exist_ok=True)
            else:
                actual_download_path = Path(download_dir_context)
            download_dir_str = str(actual_download_path)
            self.logger.debug(f"Processed download directory for engine: {download_dir_str}")

            modlist_arg = self.context.get('modlist_value') or self.context.get('machineid')
            machineid = self.context.get('machineid')

            from jackify.backend.services.nexus_auth_service import NexusAuthService
            auth_service = NexusAuthService()
            current_api_key, current_oauth_info = auth_service.get_auth_for_engine()

            api_key = current_api_key or self.context.get('nexus_api_key')
            oauth_info = current_oauth_info or self.context.get('nexus_oauth_info')

            engine_path = get_jackify_engine_path()
            engine_dir = os.path.dirname(engine_path)
            if not os.path.isfile(engine_path) or not os.access(engine_path, os.X_OK):
                print(f"{COLOR_ERROR}Jackify Install Engine not found or not executable at: {engine_path}{COLOR_RESET}")
                return

            if os.environ.get('JACKIFY_GUI_MODE') == '1':
                if not self.context.get('modlist_source'):
                    self.context['modlist_source'] = 'identifier'
                if not self.context.get('modlist_value'):
                    self.logger.error("modlist_value is missing in context for GUI workflow!")
                    return

            cmd = [engine_path, 'install', '--show-file-progress']
            modlist_value = self.context.get('modlist_value')
            if modlist_value and modlist_value.endswith('.wabbajack') and os.path.isfile(modlist_value):
                repaired = repair_local_wabbajack_if_needed(
                    Path(modlist_value),
                    logger=self.logger,
                    emit_fn=lambda msg: print(msg),
                )
                if repaired != Path(modlist_value):
                    modlist_value = str(repaired)
                    self.context['modlist_value'] = modlist_value
                cmd += ['-w', modlist_value]
            elif modlist_value:
                cmd += ['-m', modlist_value]
            elif self.context.get('machineid'):
                cmd += ['-m', self.context['machineid']]
            cmd += ['-o', install_dir_str, '-d', download_dir_str]

            from jackify.backend.handlers.config_handler import ConfigHandler
            config_handler = ConfigHandler()
            debug_mode = config_handler.get('debug_mode', False)
            if debug_mode:
                cmd.append('--debug')
                self.logger.info("Adding --debug flag to jackify-engine")

            original_env_values = {
                'NEXUS_API_KEY': os.environ.get('NEXUS_API_KEY'),
                'NEXUS_OAUTH_INFO': os.environ.get('NEXUS_OAUTH_INFO'),
                'DOTNET_SYSTEM_GLOBALIZATION_INVARIANT': os.environ.get('DOTNET_SYSTEM_GLOBALIZATION_INVARIANT')
            }

            try:
                if oauth_info:
                    os.environ['NEXUS_OAUTH_INFO'] = oauth_info
                    from jackify.backend.services.nexus_oauth_service import NexusOAuthService
                    os.environ['NEXUS_OAUTH_CLIENT_ID'] = NexusOAuthService.CLIENT_ID
                    self.logger.debug(f"Set NEXUS_OAUTH_INFO and NEXUS_OAUTH_CLIENT_ID={NexusOAuthService.CLIENT_ID} for engine (supports auto-refresh)")
                    if api_key:
                        os.environ['NEXUS_API_KEY'] = api_key
                elif api_key:
                    os.environ['NEXUS_API_KEY'] = api_key
                    self.logger.debug(f"Set NEXUS_API_KEY for engine (no auto-refresh)")
                else:
                    if 'NEXUS_API_KEY' in os.environ:
                        del os.environ['NEXUS_API_KEY']
                    if 'NEXUS_OAUTH_INFO' in os.environ:
                        del os.environ['NEXUS_OAUTH_INFO']
                    if 'NEXUS_OAUTH_CLIENT_ID' in os.environ:
                        del os.environ['NEXUS_OAUTH_CLIENT_ID']
                    self.logger.debug(f"No Nexus auth available, cleared inherited env vars")

                os.environ['DOTNET_SYSTEM_GLOBALIZATION_INVARIANT'] = "1"
                self.logger.debug(f"Temporarily set os.environ['DOTNET_SYSTEM_GLOBALIZATION_INVARIANT'] = '1' for engine call.")

                self.logger.info("Environment prepared for jackify-engine install process by modifying os.environ.")
                self.logger.debug(f"NEXUS_API_KEY in os.environ (pre-call): {'[SET]' if os.environ.get('NEXUS_API_KEY') else '[NOT SET]'}")
                self.logger.debug(f"NEXUS_OAUTH_INFO in os.environ (pre-call): {'[SET]' if os.environ.get('NEXUS_OAUTH_INFO') else '[NOT SET]'}")

                pretty_cmd = ' '.join([f'"{arg}"' if ' ' in arg else arg for arg in cmd])
                print(f"{COLOR_INFO}Launching Jackify Install Engine with command:{COLOR_RESET} {pretty_cmd}")

                from jackify.backend.handlers.subprocess_utils import increase_file_descriptor_limit
                success, old_limit, new_limit, message = increase_file_descriptor_limit()
                if success:
                    self.logger.debug(f"File descriptor limit: {message}")
                else:
                    self.logger.warning(f"File descriptor limit: {message}")

                from jackify.backend.handlers.subprocess_utils import get_clean_subprocess_env
                clean_env = get_clean_subprocess_env()
                self._current_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=False, env=clean_env, cwd=engine_dir)
                proc = self._current_process

                buffer = b''
                inline_progress_active = False
                while True:
                    chunk = proc.stdout.read(1)
                    if not chunk:
                        break
                    buffer += chunk

                    if chunk == b'\n':
                        line = buffer.decode('utf-8', errors='replace')
                        if '[FILE_PROGRESS]' in line:
                            parts = line.split('[FILE_PROGRESS]', 1)
                            if parts[0].strip():
                                line = parts[0].rstrip()
                            else:
                                buffer = b''
                                continue
                        clean_line = line.rstrip('\r\n')
                        if clean_line.startswith("Installing files "):
                            print(f"\r{clean_line}", end='')
                            sys.stdout.flush()
                            inline_progress_active = True
                        else:
                            if inline_progress_active:
                                print()
                                inline_progress_active = False
                            print(line, end='')
                        buffer = b''
                    elif chunk == b'\r':
                        line = buffer.decode('utf-8', errors='replace')
                        if '[FILE_PROGRESS]' in line:
                            parts = line.split('[FILE_PROGRESS]', 1)
                            if parts[0].strip():
                                line = parts[0].rstrip()
                            else:
                                buffer = b''
                                continue
                        clean_line = line.rstrip('\r\n')
                        if clean_line.startswith("Installing files "):
                            print(f"\r{clean_line}", end='')
                            inline_progress_active = True
                        else:
                            if inline_progress_active:
                                print()
                                inline_progress_active = False
                            print(line, end='')
                        sys.stdout.flush()
                        buffer = b''

                if buffer:
                    line = buffer.decode('utf-8', errors='replace')
                    if '[FILE_PROGRESS]' in line:
                        parts = line.split('[FILE_PROGRESS]', 1)
                        if parts[0].strip():
                            line = parts[0].rstrip()
                        else:
                            line = ''
                    if line:
                        if inline_progress_active:
                            print()
                            inline_progress_active = False
                        print(line, end='')

                if inline_progress_active:
                    print()

                proc.wait()
                self._current_process = None
                if proc.returncode != 0:
                    print(f"{COLOR_ERROR}Jackify Install Engine exited with code {proc.returncode}.{COLOR_RESET}")
                    self.logger.error(f"Engine exited with code {proc.returncode}.")
                    return
                self.logger.info(f"Engine completed with code {proc.returncode}.")
            except Exception as e:
                error_message = str(e)
                print(f"{COLOR_ERROR}Error running Jackify Install Engine: {error_message}{COLOR_RESET}\n")
                self.logger.error(f"Exception running engine: {error_message}", exc_info=True)

                try:
                    from jackify.backend.services.resource_manager import handle_file_descriptor_error
                    if any(indicator in error_message.lower() for indicator in ['too many open files', 'emfile', 'resource temporarily unavailable']):
                        result = handle_file_descriptor_error(error_message, "Jackify Install Engine execution")
                        if result['auto_fix_success']:
                            print(f"{COLOR_INFO}File descriptor limit increased automatically. {result['recommendation']}{COLOR_RESET}")
                            self.logger.info(f"File descriptor limit increased automatically. {result['recommendation']}")
                        elif result['error_detected']:
                            print(f"{COLOR_WARNING}File descriptor limit issue detected. {result['recommendation']}{COLOR_RESET}")
                            self.logger.warning(f"File descriptor limit issue detected but automatic fix failed. {result['recommendation']}")
                            if result['manual_instructions']:
                                distro = result['manual_instructions']['distribution']
                                print(f"{COLOR_INFO}Manual ulimit increase instructions available for {distro} distribution{COLOR_RESET}")
                                self.logger.info(f"Manual ulimit increase instructions available for {distro} distribution")
                except Exception as resource_error:
                    self.logger.debug(f"Error checking for resource limit issues: {resource_error}")

                return
            finally:
                for key, original_value in original_env_values.items():
                    current_value_in_os_environ = os.environ.get(key)

                    display_original_value = f"'[REDACTED]'" if key == 'NEXUS_API_KEY' else f"'{original_value}'"

                    if original_value is not None:
                        if current_value_in_os_environ != original_value:
                            os.environ[key] = original_value
                            self.logger.debug(f"Restored os.environ['{key}'] to its original value: {display_original_value}.")
                        else:
                            os.environ[key] = original_value
                            self.logger.debug(f"os.environ['{key}'] ('{display_original_value}') matched original value. Ensured restoration.")
                    else:
                        if key in os.environ:
                            self.logger.debug(f"Original os.environ['{key}'] was not set. Removing current value ('{'[REDACTED]' if os.environ.get(key) and key == 'NEXUS_API_KEY' else os.environ.get(key)}') that was set for the call.")
                            del os.environ[key]

        except Exception as e:
            error_message = str(e)
            print(f"{COLOR_ERROR}Error during installation workflow: {error_message}{COLOR_RESET}\n")
            self.logger.error(f"Exception in installation workflow: {error_message}", exc_info=True)

            try:
                from jackify.backend.services.resource_manager import handle_file_descriptor_error
                if any(indicator in error_message.lower() for indicator in ['too many open files', 'emfile', 'resource temporarily unavailable']):
                    result = handle_file_descriptor_error(error_message, "installation workflow")
                    if result['auto_fix_success']:
                        print(f"{COLOR_INFO}File descriptor limit increased automatically. {result['recommendation']}{COLOR_RESET}")
                        self.logger.info(f"File descriptor limit increased automatically. {result['recommendation']}")
                    elif result['error_detected']:
                        print(f"{COLOR_WARNING}File descriptor limit issue detected. {result['recommendation']}{COLOR_RESET}")
                        self.logger.warning(f"File descriptor limit issue detected but automatic fix failed. {result['recommendation']}")
                        if result['manual_instructions']:
                            distro = result['manual_instructions']['distribution']
                            print(f"{COLOR_INFO}Manual ulimit increase instructions available for {distro} distribution{COLOR_RESET}")
                            self.logger.info(f"Manual ulimit increase instructions available for {distro} distribution")
            except Exception as resource_error:
                self.logger.debug(f"Error checking for resource limit issues: {resource_error}")

            return
        finally:
            sys.stdout = orig_stdout
            sys.stderr = orig_stderr
            workflow_log.close()

        elapsed = int(time.time() - start_time)
        print(f"\nElapsed time: {elapsed//3600:02d}:{(elapsed%3600)//60:02d}:{elapsed%60:02d} (hh:mm:ss)\n")
        print(f"{COLOR_INFO}Your modlist has been installed to: {install_dir_str}{COLOR_RESET}\n")
        if self.context.get('machineid') != 'Tuxborn/Tuxborn':
            print(f"{COLOR_WARNING}Only Skyrim, Fallout 4, Fallout New Vegas, Oblivion, Starfield, and Oblivion Remastered modlists are compatible with Jackify's post-install configuration. Any modlist can be downloaded/installed, but only these games are supported for automated configuration.{COLOR_RESET}")

        self.logger.debug("configuration_phase: Starting post-install game detection...")

        modorganizer_ini = os.path.join(install_dir_str, "ModOrganizer.ini")
        detected_game = None
        self.logger.debug(f"configuration_phase: Looking for ModOrganizer.ini at: {modorganizer_ini}")
        if os.path.isfile(modorganizer_ini):
            self.logger.debug("configuration_phase: Found ModOrganizer.ini, detecting game...")
            from ..handlers.modlist_handler import ModlistHandler
            handler = ModlistHandler({}, steamdeck=self.steamdeck)
            handler.modlist_ini = modorganizer_ini
            handler.modlist_dir = install_dir_str
            if handler._detect_game_variables():
                detected_game = handler.game_var_full
                self.logger.debug(f"configuration_phase: Detected game: {detected_game}")
            else:
                self.logger.debug("configuration_phase: Failed to detect game variables")
        else:
            self.logger.debug("configuration_phase: ModOrganizer.ini not found")

        supported_games = ["Skyrim Special Edition", "Fallout 4", "Fallout New Vegas", "Oblivion", "Starfield", "Oblivion Remastered", "Enderal"]
        is_tuxborn = self.context.get('machineid') == 'Tuxborn/Tuxborn'
        self.logger.debug(f"configuration_phase: detected_game='{detected_game}', is_tuxborn={is_tuxborn}")
        self.logger.debug(f"configuration_phase: Checking condition: (detected_game in supported_games) or is_tuxborn")
        self.logger.debug(f"configuration_phase: Result: {(detected_game in supported_games) or is_tuxborn}")

        if (detected_game in supported_games) or is_tuxborn:
            self.logger.debug("configuration_phase: Entering Steam configuration workflow...")
            shortcut_name = self.context.get('modlist_name')
            self.logger.debug(f"configuration_phase: shortcut_name from context: '{shortcut_name}'")

            if is_tuxborn and not shortcut_name:
                self.logger.warning("Tuxborn is true, but shortcut_name (modlist_name in context) is missing. Defaulting to 'Tuxborn Automatic Installer'")
                shortcut_name = "Tuxborn Automatic Installer"
            elif not shortcut_name:
                print("\n" + "-" * 28)
                print(f"{COLOR_PROMPT}Please provide a name for the Steam shortcut for '{self.context.get('modlist_name', 'this modlist')}'.{COLOR_RESET}")
                raw_shortcut_name = input(f"{COLOR_PROMPT}Steam Shortcut Name (or 'q' to cancel): {COLOR_RESET} ").strip()
                if raw_shortcut_name.lower() == 'q' or not raw_shortcut_name:
                    self.logger.debug("configuration_phase: User cancelled shortcut name input")
                    return
                shortcut_name = raw_shortcut_name

            self.logger.debug(f"configuration_phase: Final shortcut_name: '{shortcut_name}'")

            is_gui_mode = os.environ.get('JACKIFY_GUI_MODE') == '1'
            self.logger.debug(f"configuration_phase: is_gui_mode={is_gui_mode}")

            if not is_gui_mode:
                self.logger.debug("configuration_phase: Not in GUI mode, prompting user for configuration...")
                print("\n" + "-" * 28)
                print(
                    f"{COLOR_PROMPT}Would you like to add '{shortcut_name}' to Steam and configure it now? "
                    f"Steam will restart and close any running game.{COLOR_RESET}"
                )
                configure_choice = input(f"{COLOR_PROMPT}Configure now? (Y/n): {COLOR_RESET}").strip().lower()
                self.logger.debug(f"configuration_phase: User choice: '{configure_choice}'")

                if configure_choice == 'n':
                    print(f"{COLOR_INFO}Skipping Steam configuration. You can configure it later using 'Configure New Modlist'.{COLOR_RESET}")
                    self.logger.debug("configuration_phase: User chose to skip Steam configuration")
                    return
            else:
                self.logger.debug("configuration_phase: In GUI mode, proceeding automatically...")

            self.logger.debug("configuration_phase: Proceeding with Steam configuration...")

            if not is_gui_mode:
                from jackify.backend.handlers.resolution_handler import ResolutionHandler
                resolution_handler = ResolutionHandler()

                is_steamdeck = self.steamdeck if hasattr(self, 'steamdeck') else False

                selected_resolution = resolution_handler.select_resolution(steamdeck=is_steamdeck)
                if selected_resolution:
                    self.context['resolution'] = selected_resolution
                    self.logger.info(f"Resolution set to: {selected_resolution}")

            self.logger.info(f"Starting Steam configuration for '{shortcut_name}'")

            mo2_exe_path = os.path.join(install_dir_str, 'ModOrganizer.exe')

            app_id = None
            use_automated_prefix = os.environ.get('JACKIFY_USE_AUTOMATED_PREFIX', '1') == '1'

            if use_automated_prefix:
                print(f"\n{COLOR_INFO}Using automated Steam setup workflow...{COLOR_RESET}")

                from ..services.automated_prefix_service import AutomatedPrefixService
                prefix_service = AutomatedPrefixService()

                start_time = time.time()

                def progress_callback(message):
                    noisy_patterns = (
                        "using bundled tools directory",
                        "bundled tools available",
                        "checking winetricks dependencies",
                        "(bundled)",
                        "(system)",
                        "wget",
                        "curl",
                        "aria2c",
                        "sha256sum",
                        "cabextract",
                    )
                    message_lc = message.lower()
                    if any(pattern in message_lc for pattern in noisy_patterns):
                        # Keep dependency/tool chatter in logs only for CLI readability.
                        self.logger.debug("Automated prefix detail: %s", message)
                        return

                    elapsed = time.time() - start_time
                    hours = int(elapsed // 3600)
                    minutes = int((elapsed % 3600) // 60)
                    seconds = int(elapsed % 60)
                    timestamp = f"[{hours:02d}:{minutes:02d}:{seconds:02d}]"
                    self.logger.info("Automated prefix progress: %s", message)
                    print(f"{COLOR_INFO}{timestamp} {message}{COLOR_RESET}")

                try:
                    _is_steamdeck = False
                    if os.path.exists('/etc/os-release'):
                        with open('/etc/os-release') as f:
                            if 'steamdeck' in f.read().lower():
                                _is_steamdeck = True
                except Exception:
                    _is_steamdeck = False
                result = prefix_service.run_working_workflow(
                    shortcut_name, install_dir_str, mo2_exe_path, progress_callback, steamdeck=_is_steamdeck
                )

                if isinstance(result, tuple) and len(result) == 4:
                    if result[0] == "CONFLICT":
                        conflicts = result[1]
                        print(f"\n{COLOR_WARNING}Found existing Steam shortcut(s) with the same name and path:{COLOR_RESET}")

                        for i, conflict in enumerate(conflicts, 1):
                            print(f"  {i}. Name: {conflict['name']}")
                            print(f"     Executable: {conflict['exe']}")
                            print(f"     Start Directory: {conflict['startdir']}")

                        print(f"\n{COLOR_PROMPT}Options:{COLOR_RESET}")
                        print("  * Replace - Remove the existing shortcut and create a new one")
                        print("  * Cancel - Keep the existing shortcut and stop the installation")
                        print("  * Skip - Continue without creating a Steam shortcut")

                        choice = input(f"\n{COLOR_PROMPT}Choose an option (replace/cancel/skip): {COLOR_RESET}").strip().lower()

                        if choice == 'replace':
                            print(f"{COLOR_INFO}Replacing existing shortcut...{COLOR_RESET}")
                            success, app_id = prefix_service.replace_existing_shortcut(shortcut_name, mo2_exe_path, install_dir_str)
                            if success and app_id:
                                result = prefix_service.continue_workflow_after_conflict_resolution(
                                    shortcut_name, install_dir_str, mo2_exe_path, app_id, progress_callback
                                )
                                if isinstance(result, tuple) and len(result) >= 3:
                                    success, prefix_path, app_id = result[0], result[1], result[2]
                                else:
                                    success, prefix_path, app_id = False, None, None
                            else:
                                success, prefix_path, app_id = False, None, None
                        elif choice == 'cancel':
                            print(f"{COLOR_INFO}Cancelling installation.{COLOR_RESET}")
                            return
                        elif choice == 'skip':
                            print(f"{COLOR_INFO}Skipping Steam shortcut creation.{COLOR_RESET}")
                            success, prefix_path, app_id = True, None, None
                        else:
                            print(f"{COLOR_ERROR}Invalid choice. Cancelling.{COLOR_RESET}")
                            return
                    else:
                        success, prefix_path, app_id, last_timestamp = result
                elif isinstance(result, tuple) and len(result) == 3:
                    if result[0] == "CONFLICT":
                        conflicts = result[1]
                        print(f"\n{COLOR_WARNING}Found existing Steam shortcut(s) with the same name and path:{COLOR_RESET}")

                        for i, conflict in enumerate(conflicts, 1):
                            print(f"  {i}. Name: {conflict['name']}")
                            print(f"     Executable: {conflict['exe']}")
                            print(f"     Start Directory: {conflict['startdir']}")

                        print(f"\n{COLOR_PROMPT}Options:{COLOR_RESET}")
                        print("  * Replace - Remove the existing shortcut and create a new one")
                        print("  * Cancel - Keep the existing shortcut and stop the installation")
                        print("  * Skip - Continue without creating a Steam shortcut")

                        choice = input(f"\n{COLOR_PROMPT}Choose an option (replace/cancel/skip): {COLOR_RESET}").strip().lower()

                        if choice == 'replace':
                            print(f"{COLOR_INFO}Replacing existing shortcut...{COLOR_RESET}")
                            success, app_id = prefix_service.replace_existing_shortcut(shortcut_name, mo2_exe_path, install_dir_str)
                            if success and app_id:
                                result = prefix_service.continue_workflow_after_conflict_resolution(
                                    shortcut_name, install_dir_str, mo2_exe_path, app_id, progress_callback
                                )
                                if isinstance(result, tuple) and len(result) >= 3:
                                    success, prefix_path, app_id = result[0], result[1], result[2]
                                else:
                                    success, prefix_path, app_id = False, None, None
                            else:
                                success, prefix_path, app_id = False, None, None
                        elif choice == 'cancel':
                            print(f"{COLOR_INFO}Cancelling installation.{COLOR_RESET}")
                            return
                        elif choice == 'skip':
                            print(f"{COLOR_INFO}Skipping Steam shortcut creation.{COLOR_RESET}")
                            success, prefix_path, app_id = True, None, None
                        else:
                            print(f"{COLOR_ERROR}Invalid choice. Cancelling.{COLOR_RESET}")
                            return
                    else:
                        success, prefix_path, app_id = result
                else:
                    if result is True:
                        success, prefix_path, app_id = True, None, None
                    else:
                        success, prefix_path, app_id = False, None, None

                if success:
                    print(f"{COLOR_SUCCESS}Automated Steam setup completed successfully!{COLOR_RESET}")
                    if prefix_path:
                        print(f"{COLOR_INFO}Proton prefix created at: {prefix_path}{COLOR_RESET}")
                    if app_id:
                        print(f"{COLOR_INFO}Steam AppID: {app_id}{COLOR_RESET}")
                else:
                    print(f"{COLOR_ERROR}Automated Steam setup failed. Result: {result}{COLOR_RESET}")
                    print(f"{COLOR_ERROR}Steam integration was not completed. Please check the logs for details.{COLOR_RESET}")
                    return

            from jackify.backend.services.modlist_service import ModlistService
            from jackify.backend.models.modlist import ModlistContext

            modlist_context = ModlistContext(
                name=shortcut_name,
                install_dir=Path(install_dir_str),
                download_dir=Path(install_dir_str) / "downloads",
                game_type=self.context.get('detected_game', 'Unknown'),
                nexus_api_key='',
                modlist_value=self.context.get('modlist_value', ''),
                modlist_source=self.context.get('modlist_source', 'identifier'),
                resolution=self.context.get('resolution'),
                mo2_exe_path=Path(mo2_exe_path),
                skip_confirmation=True,
                engine_installed=True
            )

            modlist_context.app_id = app_id

            modlist_service = ModlistService(self.system_info)

            if 'progress_callback' in locals() and progress_callback:
                progress_callback("")
                progress_callback("=== Configuration Phase ===")

            print(f"\n{COLOR_INFO}=== Configuration Phase ==={COLOR_RESET}")
            self.logger.info("Running post-installation configuration phase using ModlistService")

            configuration_success = modlist_service.configure_modlist_post_steam(modlist_context)

            if configuration_success:
                print(f"{COLOR_SUCCESS}Configuration completed successfully!{COLOR_RESET}")
                self.logger.info("Post-installation configuration completed successfully")
                try:
                    # Ensure CLI install flow gets the same VNV automation behavior as GUI.
                    from jackify.backend.services.vnv_integration_helper import run_vnv_automation_if_applicable
                    from jackify.backend.services.automated_prefix_service import AutomatedPrefixService

                    modlist_name_for_automation = self.context.get('modlist_name') or shortcut_name or ""
                    def _confirm_vnv(description: str) -> bool:
                        print(f"\n{description}\n")
                        try:
                            user_input = input(f"{COLOR_PROMPT}Run VNV post-install automation now? (Y/n): {COLOR_RESET}").strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            return False
                        return user_input in ("", "y", "yes")
                    def _manual_vnv_file(title: str, instructions: str):
                        print(f"\n{COLOR_WARNING}{title}{COLOR_RESET}")
                        print(instructions)
                        try:
                            file_input = input(f"{COLOR_PROMPT}Path to downloaded file: {COLOR_RESET}").strip()
                        except (EOFError, KeyboardInterrupt):
                            return None
                        if not file_input:
                            return None
                        selected = Path(file_input).expanduser().resolve()
                        return selected if selected.exists() else None
                    automation_ran, vnv_error = run_vnv_automation_if_applicable(
                        modlist_name=modlist_name_for_automation,
                        modlist_install_location=Path(install_dir_str),
                        game_root=None,  # Auto-detect from modlist structure.
                        ttw_installer_path=AutomatedPrefixService.get_ttw_installer_path(),
                        progress_callback=lambda msg: print(msg),
                        manual_file_callback=_manual_vnv_file,
                        confirmation_callback=_confirm_vnv,
                    )
                    if automation_ran and not vnv_error:
                        print(f"{COLOR_INFO}VNV post-install automation completed.{COLOR_RESET}")
                    if vnv_error:
                        print(f"{COLOR_WARNING}VNV automation encountered an error: {vnv_error}{COLOR_RESET}")
                        print(f"{COLOR_INFO}You can complete these steps manually by following: https://vivanewvegas.moddinglinked.com/wabbajack.html{COLOR_RESET}")
                except Exception as vnv_err:
                    self.logger.error("VNV post-install automation failed: %s", vnv_err, exc_info=True)
                    print(f"{COLOR_WARNING}VNV automation could not be completed. Check logs for details.{COLOR_RESET}")
                try:
                    # v0.4.0 contract: offer TTW flow for eligible FNV lists (e.g., Begin Again).
                    from jackify.backend.handlers.modlist_install_cli_ttw import prompt_ttw_if_eligible

                    prompt_ttw_if_eligible(
                        install_dir_str,
                        self.context.get('modlist_name') or shortcut_name or "",
                    )
                except Exception as ttw_err:
                    self.logger.error("TTW post-install prompt failed: %s", ttw_err, exc_info=True)
                    print(f"{COLOR_WARNING}TTW integration prompt failed. Check logs for details.{COLOR_RESET}")
            else:
                print(f"{COLOR_WARNING}Configuration had some issues but completed.{COLOR_RESET}")
                self.logger.warning("Post-installation configuration had issues")
        else:
            print(f"{COLOR_INFO}Modlist installation complete.{COLOR_RESET}")
            if detected_game:
                print(f"{COLOR_WARNING}Detected game '{detected_game}' is not supported for automated Steam configuration.{COLOR_RESET}")
            else:
                print(f"{COLOR_WARNING}Could not detect game type from ModOrganizer.ini for automated configuration.{COLOR_RESET}")
            print(f"{COLOR_INFO}You may need to manually configure the modlist for Steam/Proton.{COLOR_RESET}")
