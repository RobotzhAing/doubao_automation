"""Batch folders for finished MP4s. Recovery inputs keep their original paths."""
from collections import defaultdict
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import uuid

# Dashboard run IDs encode the local launch time. Legacy script names may
# include a descriptive run suffix before the generation/script identifier.
_RUN = re.compile(r'^(r\d{8}_\d{6}_(?:[0-9a-f]{4}(?=_|$)|[\w-]+?(?=_(?:g[0-9a-f]+_)?s\d+_)))')
_BATCH = re.compile(r'r\d{8}_\d{6}_[\w-]+\Z')
_LEGACY = re.compile(r'legacy-[0-9a-f]{24}\Z')
_FOLDER = re.compile(r'\d{4}-\d{2}-\d{2} \d{2}[:-]\d{2}\Z')


def run_id_of(name):
    match = _RUN.match(name)
    if match:
        try:
            datetime.strptime(match[1][1:16], '%Y%m%d_%H%M%S')
        except ValueError:
            return None
        return match[1]
    return None


def finished_video(path):
    name = path.name.lower()
    return (name.endswith('.mp4') and not name.endswith('.raw.mp4')
            and not name.startswith('.') and '.rendering_' not in name)


def owned_file(path):
    return (path.is_file() and not path.is_symlink()
            and not getattr(path, 'is_junction', lambda: False)())


def _legacy_id(name):
    return 'legacy-' + hashlib.sha256(name.encode('utf-8')).hexdigest()[:24]


def _date_label(batch_id, path=None):
    value = (datetime.strptime(batch_id[1:16], '%Y%m%d_%H%M%S') if not _LEGACY.fullmatch(batch_id)
             else datetime.fromtimestamp(path.stat().st_mtime))
    return value.strftime('%Y-%m-%d %H:%M')


def archive_root(root):
    return Path(root).resolve() / 'batches'


def checked_directory(path):
    # Do not open, publish into, or delete via a redirected folder.
    if (path.is_symlink() or getattr(path, 'is_junction', lambda: False)()
            or path.resolve() != path or (path.exists() and not path.is_dir())):
        raise ValueError('归档目录路径无效或已被重定向')
    return path


def archive_folder(root, batch_id, label):
    if not (_LEGACY.fullmatch(batch_id) or _BATCH.fullmatch(batch_id)):
        raise ValueError('归档批次编号无效')
    if not _FOLDER.fullmatch(label):
        raise ValueError('归档日期格式无效')
    parent = checked_directory(archive_root(root))
    batch = checked_directory(parent / batch_id)
    return checked_directory(batch / (label.replace(':', '-') if os.name == 'nt' else label))


def inventory(root):
    """Read only; includes existing archive folders to support deletion retries."""
    root = Path(root).resolve()
    grouped = defaultdict(list)
    for path in root.iterdir() if root.is_dir() else ():
        if not owned_file(path):
            continue
        if finished_video(path):
            grouped[run_id_of(path.name) or _legacy_id(path.name)].append(path)
        elif path.name.endswith(('.raw.mp4', '.raw.mp4.asset.json')) and (batch_id := run_id_of(path.name)):
            grouped.setdefault(batch_id, [])
    folders = {}
    parent = archive_root(root)
    if parent.is_dir() and not parent.is_symlink():
        for item in parent.iterdir():
            batch_id = item.name
            if not (_LEGACY.fullmatch(batch_id) or _BATCH.fullmatch(batch_id)):
                continue
            if item.is_symlink() or item.resolve() != item or not item.is_dir():
                continue
            for folder in sorted(item.iterdir()):
                if (_FOLDER.fullmatch(folder.name) and not folder.is_symlink()
                        and folder.resolve() == folder and folder.is_dir()):
                    folders[batch_id] = folder
                    break
    result = {}
    for batch_id in grouped.keys() | folders.keys():
        paths = sorted(grouped[batch_id])
        folder = folders.get(batch_id)
        try:
            label = (folder.name[:13] + ':' + folder.name[14:] if folder
                     else _date_label(batch_id, paths[0] if paths else None))
        except FileNotFoundError:
            continue
        result[batch_id] = dict(id=batch_id, label=label, paths=paths, folder=folder)
    return result


def publish(root, batch_id, label, paths):
    """Link only finished files, without copying bytes or changing journal paths."""
    folder = archive_folder(root, batch_id, label)
    folder.mkdir(parents=True, exist_ok=True)
    for source in paths:
        if (source.parent != Path(root).resolve() or not finished_video(source)
                or not owned_file(source) or (run_id_of(source.name) or _legacy_id(source.name)) != batch_id):
            continue
        target = folder / source.name
        if target.is_symlink() or (target.exists() and not owned_file(target)):
            raise ValueError('归档视频路径已被重定向')
        try:
            if source.samefile(target):
                continue
        except FileNotFoundError:
            pass
        temp = folder.parent / f'.{uuid.uuid4().hex}.link'
        try:
            os.link(source, temp)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
    return folder


def publish_finished(root, source):
    source = Path(source).parent.resolve() / Path(source).name
    if source.parent != Path(root).resolve():
        raise ValueError('成片不在下载目录')
    batch_id = run_id_of(source.name) or _legacy_id(source.name)
    return publish(root, batch_id, _date_label(batch_id, source), [source])


def backfill(root):
    batches = inventory(root)
    for batch in batches.values():
        publish(root, batch['id'], batch['label'], batch['paths'])
    return len(batches)


def belongs(batch, name):
    if not _LEGACY.fullmatch(batch['id']):
        return run_id_of(name) == batch['id']
    # Legacy files without a launch ID get one group per original finished file.
    stem, separator, _ = name.rpartition('.mp4')
    if not separator:
        stem = name.removesuffix('.processing.json').removesuffix('.json')
    return _legacy_id(stem.removesuffix('.raw') + '.mp4') == batch['id']


def batch_files(root, batch):
    root = Path(root).resolve()
    for directory in (root, root / '.download-backups', root / 'approved'):
        if directory.is_symlink() or directory.resolve() != directory or not directory.is_dir():
            continue
        for path in directory.iterdir():
            if belongs(batch, path.name) and owned_file(path) and (
                    path.suffix.lower() in ('.mp4', '.json', '.unverified')):
                yield path


def archived_copy(root, source):
    batch_id = run_id_of(source.name) or _legacy_id(source.name)
    batch = inventory(root).get(batch_id)
    if batch and batch['folder']:
        folder = archive_folder(root, batch_id, batch['label'])
        return folder / source.name
    return None
