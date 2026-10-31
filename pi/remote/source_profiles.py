"""Keep source selections in checked working copies of immutable DSP presets."""
import hashlib
import json
from pathlib import Path
import re


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class SourceProfiles:
    def __init__(self, root):
        self.root = Path(root).absolute()

    def _working(self, path):
        path = Path(path).absolute()
        return (path.parent == self.root and
                re.fullmatch(r'source-[0-9a-f]{24}\.yml', path.name) is not None)

    def target(self, origin_path):
        """Give each approved origin its own mutable source configuration."""
        origin = Path(origin_path).absolute()
        if self._working(origin):
            raise ValueError('A source working copy cannot be used as an origin')
        key = hashlib.sha256(str(origin).encode()).hexdigest()[:24]
        return self.root / f'source-{key}.yml'

    def manifest_path(self, working_path):
        path = Path(working_path).absolute()
        if not self._working(path):
            raise ValueError('Expected a managed source working copy')
        return path.with_suffix('.source.json')

    def read(self, working_path, approve_origin=None):
        """Return provenance only while both files and the origin remain trusted.

        ``approve_origin`` receives the original config path and may return its
        current approval metadata. That result is returned as ``origin_approval``
        but never persisted, so an obsolete saved approval cannot confer trust.
        """
        try:
            path = Path(working_path).absolute()
            if not self._working(path):
                return None
            record = json.loads(self.manifest_path(path).read_text(encoding='utf-8'))
            if not isinstance(record, dict) or record.get('version') != 1:
                return None
            origin = Path(record['origin_path'])
            # Identity is by path; file contents are not checksum-checked.
            if (not origin.is_absolute() or self._working(origin) or
                    self.target(origin) != path or record['config_path'] != str(path) or
                    not origin.is_file() or not path.is_file()):
                return None
            approval = approve_origin(str(origin)) if approve_origin else True
            if not approval:
                return None
            return dict(record, origin_approval=approval)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            return None

    def origin(self, working_path):
        record = self.read(working_path)
        return Path(record['origin_path']) if record else None

    def save(self, working_path, origin_path):
        """Publish provenance after a validated working file has been installed.

        The caller owns validation, activation and rollback of the configuration
        and this manifest together. Immutable origin files are only read here.
        """
        path, origin = Path(working_path).absolute(), Path(origin_path).absolute()
        if self.target(origin) != path:
            raise ValueError('Working copy does not belong to this origin')
        record = dict(version=1, config_path=str(path), sha256=digest(path),
                      origin_path=str(origin), origin_sha256=digest(origin))
        manifest = self.manifest_path(path)
        temporary = manifest.with_suffix('.tmp')
        try:
            temporary.write_text(json.dumps(record, indent=2), encoding='utf-8')
            temporary.chmod(0o640)
            temporary.replace(manifest)
        finally:
            temporary.unlink(missing_ok=True)
        return record
