"""Keep Unicode-path support without discarding vendor executable settings."""

import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def test_utf8_manifest_preserves_vendor_execution_and_dependencies(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    from build_windows_webui import utf8_manifest

    source = b"""<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
      <assemblyIdentity name="PostgreSQL" version="16.0.0.0" type="win32"/>
      <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security>
        <requestedPrivileges><requestedExecutionLevel level="asInvoker" uiAccess="false"/>
        </requestedPrivileges></security></trustInfo>
      <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
        <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
      </windowsSettings></application>
      <dependency><dependentAssembly><assemblyIdentity name="vendor-runtime"/>
        </dependentAssembly></dependency></assembly>"""
    result = ET.fromstring(utf8_manifest(utf8_manifest(source)))
    assert result.find("{*}assemblyIdentity").get("name") == "PostgreSQL"
    assert result.find(".//{*}requestedExecutionLevel").get("level") == "asInvoker"
    assert (
        result.find(".//{*}dependency/{*}dependentAssembly/{*}assemblyIdentity").get("name")
        == "vendor-runtime"
    )
    assert result.find(".//{*}longPathAware").text == "true"
    code_pages = result.findall(
        ".//{http://schemas.microsoft.com/SMI/2019/WindowsSettings}activeCodePage"
    )
    assert len(code_pages) == 1 and code_pages[0].text == "UTF-8"
    assert len(result.findall("{urn:schemas-microsoft-com:asm.v3}application")) == 1


def test_owned_process_redirects_output_and_reports_exit(tmp_path):
    from wenyi_api.desktop.platform import ProcessOwner

    owner = ProcessOwner()
    try:
        with (tmp_path / "process.log").open("wb") as output:
            child = owner.spawn(
                [sys.executable, "-c", "print('owned child')"],
                output=output,
                env=os.environ.copy(),
                cwd=tmp_path,
                restrict_admin=True,
            )
            assert child.wait(timeout=10) == 0
            assert child.poll() == 0
        assert (tmp_path / "process.log").read_text().strip() == "owned child"
    finally:
        owner.close()
