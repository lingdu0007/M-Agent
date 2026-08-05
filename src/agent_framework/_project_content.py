from pathlib import Path
from typing import Iterable, Iterator, Union


class ProjectContent:
    """Resolve and enumerate ordinary files below one resolved Project Root."""

    def __init__(self, root: Union[str, Path]) -> None:
        self._selected_root = Path(root).absolute()
        self.root = self._selected_root.resolve()

    def iter_files(self, ignores: Iterable[str]) -> Iterator[Path]:
        ignored = set(ignores)
        for path in sorted(self.root.rglob("*")):
            if any(
                part in ignored or part.endswith(".egg-info")
                for part in path.parts
            ):
                continue
            if self._contains_symbolic_link(path):
                continue
            if path.is_file():
                yield path

    def resolve_file(self, path: Union[str, Path]) -> Path:
        requested = Path(path)
        if requested.is_absolute():
            relative = self._relative_to_selected_root(requested)
            candidate = self.root / relative
        else:
            candidate = self.root / requested
        target = candidate.resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"Path outside project root: {path}") from exc

        try:
            contains_symbolic_link = self._contains_symbolic_link(candidate)
        except ValueError as exc:
            raise ValueError(f"Path outside project root: {path}") from exc
        if contains_symbolic_link:
            raise ValueError(f"Symbolic links are not project content: {path}")
        return target

    def _relative_to_selected_root(self, path: Path) -> Path:
        for root in (self._selected_root, self.root):
            try:
                return path.relative_to(root)
            except ValueError:
                continue
        raise ValueError(f"Path outside project root: {path}")

    def _contains_symbolic_link(self, path: Path) -> bool:
        current = self.root
        for part in path.relative_to(self.root).parts:
            current /= part
            if current.is_symlink():
                return True
        return False
