"""
InstallerThread: QThread subclass for running jackify-engine install.
Signals are defined at class level (required for Qt signal/slot).
"""

import os
import re
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal
import logging

from jackify.backend.utils.engine_error_parser import parse_engine_error_line, error_from_exit_code
from jackify.backend.utils.wabbajack_inline_repair import repair_local_wabbajack_if_needed
from jackify.shared.errors import JackifyError


logger = logging.getLogger(__name__)
class InstallerThread(QThread):
    """Runs jackify-engine install in a background thread. Signals at class level."""

    output_received = Signal(str)
    progress_received = Signal(str)
    progress_updated = Signal(object)
    installation_finished = Signal(bool, str)
    premium_required_detected = Signal(str)

    def __init__(self, modlist, install_dir, downloads_dir, api_key, modlist_name,
                 install_mode='online', progress_state_manager=None, auth_service=None, oauth_info=None):
        super().__init__()
        self.modlist = modlist
        self.install_dir = install_dir
        self.downloads_dir = downloads_dir
        self.api_key = api_key
        self.modlist_name = modlist_name
        self.install_mode = install_mode
        self.cancelled = False
        self.process_manager = None
        self.progress_state_manager = progress_state_manager
        self.auth_service = auth_service
        self.oauth_info = oauth_info
        self._premium_signal_sent = False
        self._engine_output_buffer = []
        self._buffer_size = 10
        self.last_error: Optional[JackifyError] = None
        self._raw_stderr_lines: list = []  # bounded ring buffer for non-JSON stderr

    def cancel(self):
        self.cancelled = True
        if self.process_manager:
            self.process_manager.cancel()

    def _read_stderr(self):
        try:
            for raw in self.process_manager.proc.stderr:
                line = raw.decode('utf-8', errors='replace').strip()
                if not line:
                    continue
                logger.debug(f"Engine stderr: {line}")
                error = parse_engine_error_line(line)
                if error and self.last_error is None:
                    self.last_error = error
                else:
                    self._raw_stderr_lines.append(line)
                    if len(self._raw_stderr_lines) > 20:
                        self._raw_stderr_lines.pop(0)
        except Exception as e:
            logger.debug(f"Stderr reader error: {e}")

    def run(self):
        try:
            from jackify.backend.core.modlist_operations import get_jackify_engine_path
            engine_path = get_jackify_engine_path()
            if not os.path.exists(engine_path):
                error_msg = f"Engine not found at: {engine_path}"
                logger.debug(f"DEBUG: {error_msg}")
                self.installation_finished.emit(False, error_msg)
                return
            if not os.access(engine_path, os.X_OK):
                error_msg = f"Engine is not executable: {engine_path}"
                logger.debug(f"DEBUG: {error_msg}")
                self.installation_finished.emit(False, error_msg)
                return
            logger.debug(f"DEBUG: Using engine at: {engine_path}")
            if self.install_mode == 'file' and isinstance(self.modlist, str) and self.modlist.endswith('.wabbajack') and os.path.isfile(self.modlist):
                repaired = repair_local_wabbajack_if_needed(
                    Path(self.modlist),
                    logger=logger,
                    emit_fn=lambda msg: self.output_received.emit(msg + "\n"),
                )
                if repaired != Path(self.modlist):
                    self.modlist = str(repaired)
            if self.install_mode == 'file':
                cmd = [engine_path, "install", "--show-file-progress", "-w", self.modlist, "-o", self.install_dir, "-d", self.downloads_dir]
            else:
                cmd = [engine_path, "install", "--show-file-progress", "-m", self.modlist, "-o", self.install_dir, "-d", self.downloads_dir]
            from jackify.backend.handlers.config_handler import ConfigHandler
            config_handler = ConfigHandler()
            debug_mode = config_handler.get('debug_mode', False)
            if debug_mode:
                cmd.append('--debug')
                logger.debug("DEBUG: Added --debug flag to jackify-engine command")
            logger.debug(f"DEBUG: FULL Engine command: {' '.join(cmd)}")
            logger.debug(f"DEBUG: modlist value being passed: '{self.modlist}'")
            from jackify.backend.handlers.subprocess_utils import get_clean_subprocess_env
            env_vars = {'NEXUS_API_KEY': self.api_key}
            if self.oauth_info:
                env_vars['NEXUS_OAUTH_INFO'] = self.oauth_info
                from jackify.backend.services.nexus_oauth_service import NexusOAuthService
                env_vars['NEXUS_OAUTH_CLIENT_ID'] = NexusOAuthService.CLIENT_ID
            env = get_clean_subprocess_env(env_vars)
            from jackify.backend.handlers.subprocess_utils import ProcessManager
            self.process_manager = ProcessManager(cmd, env=env, text=False, separate_stderr=True)
            stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
            stderr_thread.start()
            ansi_escape = re.compile(rb'\x1b\[[0-9;?]*[ -/]*[@-~]')
            buffer = b''
            last_was_blank = False
            while True:
                if self.cancelled:
                    self.cancel()
                    break
                char = self.process_manager.read_stdout_char()
                if not char:
                    break
                buffer += char
                while b'\n' in buffer or b'\r' in buffer:
                    if b'\r' in buffer and (buffer.index(b'\r') < buffer.index(b'\n') if b'\n' in buffer else True):
                        line, buffer = buffer.split(b'\r', 1)
                        line = ansi_escape.sub(b'', line)
                        decoded = line.decode('utf-8', errors='replace')
                        config_handler = ConfigHandler()
                        debug_mode = config_handler.get('debug_mode', False)
                        from jackify.backend.utils.nexus_premium_detector import is_non_premium_indicator
                        is_premium_error, matched_pattern = is_non_premium_indicator(decoded)
                        if not self._premium_signal_sent and is_premium_error:
                            self._premium_signal_sent = True
                            logger.warning("=" * 80)
                            logger.warning("PREMIUM DETECTION TRIGGERED - DIAGNOSTIC DUMP (Issue #111)")
                            logger.warning("=" * 80)
                            logger.warning(f"Matched pattern: '{matched_pattern}'")
                            logger.warning(f"Triggering line: '{decoded.strip()}'")
                            logger.warning("AUTHENTICATION DIAGNOSTICS:")
                            logger.warning(f"  Auth value present: {'YES' if self.api_key else 'NO'}")
                            if self.api_key:
                                logger.warning(f"  Auth value length: {len(self.api_key)} chars")
                                if len(self.api_key) >= 8:
                                    logger.warning(f"  Auth value (partial): {self.api_key[:4]}...{self.api_key[-4:]}")
                                auth_method = self.auth_service.get_auth_method() if self.auth_service else None
                                logger.warning(f"  Auth method: {auth_method or 'UNKNOWN'}")
                                if auth_method == 'oauth' and self.auth_service:
                                    token_handler = self.auth_service.token_handler
                                    token_info = token_handler.get_token_info()
                                    logger.warning("  OAuth Token Status:")
                                    logger.warning(f"    Has token file: {token_info.get('has_token', False)}")
                                    logger.warning(f"    Has refresh token: {token_info.get('has_refresh_token', False)}")
                                    if 'expires_in_minutes' in token_info:
                                        logger.warning(f"    Expires in: {token_info['expires_in_minutes']:.1f} minutes")
                                    if 'refresh_token_age_days' in token_info:
                                        logger.warning(f"    Refresh token age: {token_info['refresh_token_age_days']:.1f} days")
                                    if token_info.get('error'):
                                        logger.warning(f"    Error: {token_info['error']}")
                            logger.warning("Previous engine output (last 10 lines):")
                            for i, buffered_line in enumerate(self._engine_output_buffer, 1):
                                logger.warning(f"  -{len(self._engine_output_buffer) - i + 1}: {buffered_line}")
                            logger.warning("If user HAS Premium, this is a FALSE POSITIVE")
                            logger.warning("=" * 80)
                            self.premium_required_detected.emit(decoded.strip() or "Nexus Premium required")
                        self._engine_output_buffer.append(decoded.strip())
                        if len(self._engine_output_buffer) > self._buffer_size:
                            self._engine_output_buffer.pop(0)
                        if self.progress_state_manager:
                            updated = self.progress_state_manager.process_line(decoded)
                            if updated:
                                progress_state = self.progress_state_manager.get_state()
                                if progress_state.active_files and debug_mode:
                                    logger.debug(f"DEBUG: Parser detected {len(progress_state.active_files)} active files from line: {decoded[:80]}")
                                self.progress_updated.emit(progress_state)
                        if '[FILE_PROGRESS]' in decoded:
                            parts = decoded.split('[FILE_PROGRESS]', 1)
                            if parts[0].strip():
                                self.progress_received.emit(parts[0].rstrip())
                        else:
                            self.progress_received.emit(decoded + '\r')
                    elif b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        line = ansi_escape.sub(b'', line)
                        decoded = line.decode('utf-8', errors='replace')
                        from jackify.backend.utils.nexus_premium_detector import is_non_premium_indicator
                        is_premium_error, matched_pattern = is_non_premium_indicator(decoded)
                        if not self._premium_signal_sent and is_premium_error:
                            self._premium_signal_sent = True
                            logger.warning("=" * 80)
                            logger.warning("PREMIUM DETECTION TRIGGERED - DIAGNOSTIC DUMP (Issue #111)")
                            logger.warning("=" * 80)
                            logger.warning(f"Matched pattern: '{matched_pattern}'")
                            logger.warning(f"Triggering line: '{decoded.strip()}'")
                            logger.warning("AUTHENTICATION DIAGNOSTICS:")
                            logger.warning(f"  Auth value present: {'YES' if self.api_key else 'NO'}")
                            if self.api_key:
                                logger.warning(f"  Auth value length: {len(self.api_key)} chars")
                                if len(self.api_key) >= 8:
                                    logger.warning(f"  Auth value (partial): {self.api_key[:4]}...{self.api_key[-4:]}")
                                auth_method = self.auth_service.get_auth_method() if self.auth_service else None
                                logger.warning(f"  Auth method: {auth_method or 'UNKNOWN'}")
                                if auth_method == 'oauth' and self.auth_service:
                                    token_handler = self.auth_service.token_handler
                                    token_info = token_handler.get_token_info()
                                    logger.warning("  OAuth Token Status:")
                                    logger.warning(f"    Has token file: {token_info.get('has_token', False)}")
                                    logger.warning(f"    Has refresh token: {token_info.get('has_refresh_token', False)}")
                                    if 'expires_in_minutes' in token_info:
                                        logger.warning(f"    Expires in: {token_info['expires_in_minutes']:.1f} minutes")
                                    if 'refresh_token_age_days' in token_info:
                                        logger.warning(f"    Refresh token age: {token_info['refresh_token_age_days']:.1f} days")
                                    if token_info.get('error'):
                                        logger.warning(f"    Error: {token_info['error']}")
                            logger.warning("Previous engine output (last 10 lines):")
                            for i, buffered_line in enumerate(self._engine_output_buffer, 1):
                                logger.warning(f"  -{len(self._engine_output_buffer) - i + 1}: {buffered_line}")
                            logger.warning("If user HAS Premium, this is a FALSE POSITIVE")
                            logger.warning("=" * 80)
                            self.premium_required_detected.emit(decoded.strip() or "Nexus Premium required")
                        self._engine_output_buffer.append(decoded.strip())
                        if len(self._engine_output_buffer) > self._buffer_size:
                            self._engine_output_buffer.pop(0)
                        config_handler = ConfigHandler()
                        debug_mode = config_handler.get('debug_mode', False)
                        if self.progress_state_manager:
                            updated = self.progress_state_manager.process_line(decoded)
                            if updated:
                                progress_state = self.progress_state_manager.get_state()
                                if progress_state.active_files and debug_mode:
                                    logger.debug(f"DEBUG: Parser detected {len(progress_state.active_files)} active files from line: {decoded[:80]}")
                                self.progress_updated.emit(progress_state)
                        if '[FILE_PROGRESS]' in decoded:
                            parts = decoded.split('[FILE_PROGRESS]', 1)
                            if parts[0].strip():
                                self.output_received.emit(parts[0].rstrip())
                            last_was_blank = False
                            continue
                        if decoded.strip() == '':
                            if not last_was_blank:
                                self.output_received.emit('\n')
                            last_was_blank = True
                        else:
                            self.output_received.emit(decoded + '\n')
                            last_was_blank = False
            if buffer:
                line = ansi_escape.sub(b'', buffer)
                decoded = line.decode('utf-8', errors='replace')
                if '[FILE_PROGRESS]' in decoded:
                    parts = decoded.split('[FILE_PROGRESS]', 1)
                    if parts[0].strip():
                        self.output_received.emit(parts[0].rstrip())
                else:
                    self.output_received.emit(decoded)
            stderr_thread.join(timeout=5)
            returncode = self.process_manager.wait()
            if self.process_manager.proc and self.process_manager.proc.stdout:
                try:
                    remaining = self.process_manager.proc.stdout.read()
                    if remaining:
                        decoded_remaining = remaining.decode('utf-8', errors='replace')
                        if decoded_remaining.strip():
                            logger.debug(f"DEBUG: Remaining output after process exit: {decoded_remaining[:500]}")
                            if '[FILE_PROGRESS]' in decoded_remaining:
                                parts = decoded_remaining.split('[FILE_PROGRESS]', 1)
                                if parts[0].strip():
                                    self.output_received.emit(parts[0].rstrip())
                            else:
                                self.output_received.emit(decoded_remaining)
                except Exception as e:
                    logger.debug(f"DEBUG: Error reading remaining output: {e}")
            if returncode != 0 and not self.cancelled and self.last_error is None:
                stderr_detail = "\n".join(self._raw_stderr_lines[-10:]) if self._raw_stderr_lines else ""
                detail = f"Exit code {returncode}.\n\nEngine output:\n{stderr_detail}" if stderr_detail else f"Exit code {returncode}."
                fallback = error_from_exit_code(
                    returncode,
                    detail,
                    context={
                        "exit_code": returncode,
                        "stderr_tail_lines": len(self._raw_stderr_lines[-10:]),
                    },
                )
                if fallback:
                    self.last_error = fallback

            if self.cancelled:
                self.installation_finished.emit(False, "Installation cancelled by user")
            elif returncode == 0:
                self.installation_finished.emit(True, "Installation completed successfully")
            else:
                error_msg = f"Installation failed (exit code {returncode})"
                logger.debug(f"DEBUG: Engine exited with code {returncode}")
                self.installation_finished.emit(False, error_msg)
        except Exception as e:
            self.installation_finished.emit(False, f"Installation error: {str(e)}")
        finally:
            if self.cancelled and self.process_manager:
                self.process_manager.cancel()
