import re
import json
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

MUSIC_ROOT = "/home/madmax/Musik/mp3 Musik"
SUPPORTED_EXTENSIONS = {".mp3", ".m4a", ".ogg", ".flac", ".wav"}


def _parse_prefix(name: str) -> tuple[int, str]:
    """Extract numeric sort index and clean display name.

    '01-Sandmann'            -> (1, 'Sandmann')
    '04 Ein Männlein Steht'  -> (4, 'Ein Männlein Steht')
    '001'                    -> (1, '001')
    'No prefix'              -> (0, 'No prefix')
    """
    match = re.match(r'^(\d+)[-\s]+(.*)', name)
    if match:
        return int(match.group(1)), match.group(2).strip()
    # numeric-only filename (e.g. '001')
    match = re.match(r'^(\d+)$', name)
    if match:
        return int(match.group(1)), name
    return 0, name.strip()


@dataclass
class Track:
    id: str       # relative path from music root — stable unique ID
    title: str
    path: str     # absolute path
    index: int    # sort order within album


@dataclass
class Album:
    id: str       # slugified folder name
    name: str
    path: str
    index: int    # sort order in library
    tracks: list[Track] = field(default_factory=list)


@dataclass
class Library:
    albums: list[Album] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"albums": [asdict(a) for a in self.albums]}

    def find_album(self, album_id: str) -> Optional[Album]:
        return next((a for a in self.albums if a.id == album_id), None)

    def find_track(self, track_id: str) -> Optional[Track]:
        for album in self.albums:
            for track in album.tracks:
                if track.id == track_id:
                    return track
        return None


def scan(root: str = MUSIC_ROOT) -> Library:
    """Scan the music root directory and return a Library."""
    library = Library()
    root_path = Path(root)

    if not root_path.exists():
        return library

    for folder in sorted(root_path.iterdir()):
        if not folder.is_dir():
            continue

        index, name = _parse_prefix(folder.name)
        album_id = re.sub(r'[^\w-]', '-', folder.name.lower()).strip('-')
        album = Album(id=album_id, name=name, path=str(folder), index=index)

        for file in sorted(folder.iterdir()):
            if file.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if file.name.startswith('._'):  # skip macOS metadata files
                continue

            track_index, track_title = _parse_prefix(file.stem)
            track_id = str(file.relative_to(root_path))
            album.tracks.append(Track(
                id=track_id,
                title=track_title,
                path=str(file),
                index=track_index,
            ))

        album.tracks.sort(key=lambda t: t.index)

        if album.tracks:
            library.albums.append(album)

    library.albums.sort(key=lambda a: a.index)
    return library


if __name__ == "__main__":
    lib = scan()
    print(json.dumps(lib.to_dict(), ensure_ascii=False, indent=2))
