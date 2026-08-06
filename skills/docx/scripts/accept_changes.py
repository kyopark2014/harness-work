"""Accept all tracked changes in a DOCX file using LibreOffice.

Requires LibreOffice (soffice) to be installed.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from office.soffice import get_soffice_env

logger = logging.getLogger(__name__)

_libreoffice_profile: str | None = None


def _libreoffice_profile_dir() -> str:
    """Return a unique LibreOffice user profile under the system temp dir."""
    global _libreoffice_profile
    if _libreoffice_profile is None:
        _libreoffice_profile = tempfile.mkdtemp(prefix="libreoffice_docx_profile_")
    return _libreoffice_profile


def _macro_dir() -> Path:
    return Path(_libreoffice_profile_dir()) / "user" / "basic" / "Standard"

ACCEPT_CHANGES_MACRO = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE script:module PUBLIC "-//OpenOffice.org//DTD OfficeDocument 1.0//EN" "module.dtd">
<script:module xmlns:script="http://openoffice.org/2000/script" script:name="Module1" script:language="StarBasic">
    Sub AcceptAllTrackedChanges()
        Dim document As Object
        Dim dispatcher As Object

        document = ThisComponent.CurrentController.Frame
        dispatcher = createUnoService("com.sun.star.frame.DispatchHelper")

        dispatcher.executeDispatch(document, ".uno:AcceptAllTrackedChanges", "", 0, Array())
        ThisComponent.store()
        ThisComponent.close(True)
    End Sub
</script:module>"""


SOFFICE_BIN = "soffice"
SOFFICE_TIMEOUT_SECONDS = 30
MACRO_INIT_TIMEOUT_SECONDS = 10  # short timeout for soffice profile init only


def _resolve_docx_path(path_str: str) -> Path | None:
    """Resolve path and require a .docx suffix (blocks odd argv injection shapes)."""
    try:
        resolved = Path(path_str).expanduser().resolve()
    except OSError:
        return None
    if resolved.suffix.lower() != ".docx":
        return None
    return resolved


def accept_changes(
    input_file: str,
    output_file: str,
) -> tuple[None, str]:
    input_path = _resolve_docx_path(input_file)
    output_path = _resolve_docx_path(output_file)

    if input_path is None:
        logger.warning("Input file is not a valid DOCX path: %s", input_file)
        return None, "Error: Input file is not a valid DOCX path"
    if output_path is None:
        logger.warning("Output file is not a valid DOCX path: %s", output_file)
        return None, "Error: Output file is not a valid DOCX path"

    if not input_path.exists():
        logger.warning("Input file not found: %s", input_file)
        return None, "Error: Input file not found"

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(input_path, output_path)
    except Exception as e:
        logger.warning("Failed to copy input file to output location: %s", e)
        return None, "Error: Failed to copy input file to output location"

    if not _setup_libreoffice_macro():
        return None, "Error: Failed to setup LibreOffice macro"

    # Fixed binary + resolved path argv (shell=False). Dynamic path is validated above.
    profile_dir = _libreoffice_profile_dir()
    cmd = [
        SOFFICE_BIN,
        "--headless",
        f"-env:UserInstallation=file://{profile_dir}",
        "--norestore",
        "vnd.sun.star.script:Standard.Module1.AcceptAllTrackedChanges?language=Basic&location=application",
        str(output_path),
    ]

    try:
        # Fixed `soffice` binary + validated .docx path argv list, shell=False.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        result = subprocess.run(  # nosec B603 — fixed `soffice` binary + validated .docx path argv list, shell=False
            cmd,
            capture_output=True,
            text=True,
            timeout=SOFFICE_TIMEOUT_SECONDS,
            check=False,
            env=get_soffice_env(),
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "LibreOffice timed out after %ss processing: %s",
            SOFFICE_TIMEOUT_SECONDS,
            input_file,
        )
        return None, "Error: LibreOffice document conversion timed out"
    except FileNotFoundError:
        return None, "Error: LibreOffice (soffice) not found"
    except OSError as e:
        logger.warning("LibreOffice OSError: %s", e)
        return None, f"Error: LibreOffice OS error ({type(e).__name__})"

    if result.returncode != 0:
        logger.warning("LibreOffice failed: %s", result.stderr)
        return None, "Error: LibreOffice command failed"

    return (
        None,
        f"Successfully accepted all tracked changes: {input_file} -> {output_file}",
    )


def _setup_libreoffice_macro() -> bool:
    profile_dir = _libreoffice_profile_dir()
    macro_dir = _macro_dir()
    macro_file = macro_dir / "Module1.xba"

    if macro_file.exists() and "AcceptAllTrackedChanges" in macro_file.read_text():
        return True

    if not macro_dir.exists():
        # Fixed `soffice` binary argv list for profile init, shell=False.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        subprocess.run(  # nosec B603 — fixed `soffice` binary argv list for profile init, shell=False
            [
                SOFFICE_BIN,
                "--headless",
                f"-env:UserInstallation=file://{profile_dir}",
                "--terminate_after_init",
            ],
            capture_output=True,
            timeout=MACRO_INIT_TIMEOUT_SECONDS,
            check=False,
            env=get_soffice_env(),
        )
        macro_dir.mkdir(parents=True, exist_ok=True)

    try:
        macro_file.write_text(ACCEPT_CHANGES_MACRO)
        return True
    except Exception:
        logger.exception("Failed to setup LibreOffice macro")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Accept all tracked changes in a DOCX file"
    )
    parser.add_argument("input_file", help="Input DOCX file with tracked changes")
    parser.add_argument(
        "output_file", help="Output DOCX file (clean, no tracked changes)"
    )
    args = parser.parse_args()

    _, message = accept_changes(args.input_file, args.output_file)
    print(message)

    if "Error" in message:
        raise SystemExit(1)
