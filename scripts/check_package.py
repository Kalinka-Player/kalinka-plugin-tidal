"""Inspect the release package without installing or executing its contents."""

import configparser
import hashlib
import io
import json
import platform
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version


def inspect(path: Path):
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    fields = {}
    for field in ("Package", "Version", "Architecture", "Depends"):
        fields[field] = subprocess.check_output(
            ["dpkg-deb", "-f", str(path), field], text=True
        ).strip()
    assert fields["Package"] == project["name"]
    assert fields["Architecture"] == "all"
    assert "kalinka-server (>= 5.11.0)" in fields["Depends"]
    assert "kalinka-server (<< 6)" in fields["Depends"]
    assert "kalinka-plugin-sdk (>= 3.18)" in fields["Depends"]
    assert "kalinka-plugin-sdk (<< 4)" in fields["Depends"]
    assert "python3 (>= 3.11)" in fields["Depends"]
    data = subprocess.check_output(["dpkg-deb", "--fsys-tarfile", str(path)])
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        files = [member for member in archive if member.isfile()]
        assert len(files) == 1, "Only the plugin wheel belongs in the package"
        assert files[0].name.startswith("./opt/kalinka/wheels/kalinka_plugin_tidal-")
        assert files[0].name.endswith("-py3-none-any.whl")
        wheel_bytes = archive.extractfile(files[0]).read()
    with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as wheel:
        metadata = Parser().parsestr(
            wheel.read(
                next(
                    name
                    for name in wheel.namelist()
                    if name.endswith(".dist-info/METADATA")
                )
            ).decode()
        )
        version = Version(metadata["Version"])
        deb_version = str(version).replace(".dev", "~dev")
        assert fields["Version"] == deb_version
        assert metadata["Name"] == project["name"]
        assert metadata["Requires-Python"] == project["requires-python"]
        requirements = {
            Requirement(value).name: Requirement(value).specifier
            for value in metadata.get_all("Requires-Dist")
        }
        for dependency in project["dependencies"]:
            expected = Requirement(dependency)
            assert requirements[expected.name] == expected.specifier
        entries = configparser.ConfigParser()
        entries.read_string(
            wheel.read(
                next(
                    name
                    for name in wheel.namelist()
                    if name.endswith(".dist-info/entry_points.txt")
                )
            ).decode()
        )
        assert (
            dict(entries["kalinka.plugins"])
            == project["entry-points"]["kalinka.plugins"]
        )
    control = subprocess.check_output(["dpkg-deb", "--ctrl-tarfile", str(path)])
    with tarfile.open(fileobj=io.BytesIO(control)) as archive:
        for name in ("prerm", "triggers"):
            assert (
                archive.extractfile("./" + name).read()
                == Path("debian", name).read_bytes()
            )
        assert archive.getmember("./prerm").mode & 0o111
        assert not any(member.name == "./postinst" for member in archive)
    return {
        "package": fields,
        "python_version": str(version),
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "build_architecture": platform.machine(),
        "build_python": platform.python_version(),
        "build_os": platform.freedesktop_os_release(),
    }


if __name__ == "__main__":
    print(json.dumps(inspect(Path(sys.argv[1])), indent=2))
