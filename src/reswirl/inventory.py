# src/reswirl/inventory.py
from __future__ import annotations

import os
from pathlib import Path

import polars as pl
from github import Github
from platformdirs import user_cache_dir
from upath import UPath

ENV_GH_TOKEN = os.getenv("GITHUB_TOKEN")


class Inventory:
    """
    Provides functionality to retrieve and parse a GitHub user's public repositories
    into a Polars DataFrame, caching results locally to avoid repeated API calls.
    """

    def __init__(
        self,
        username: str,
        lazy: bool = False,
        token: str | None = None,
        use_cache: bool = True,
        force_refresh: bool = False,
        repo_filter: str | pl.Expr | None = None,
        tree_filter: str | pl.Expr | None = None,
    ) -> None:
        """
        Initialise the Inventory object.

        Args:
            username: The GitHub username to fetch repositories for.
            lazy: Whether to allow lazy Polars operations (not all transformations may be supported).
            token: An optional GitHub personal access token for higher rate limits.
            use_cache: Whether to use cached results if available.
            force_refresh: If True, always refetch from GitHub and overwrite the cache.
            repo_filter: Either a Polars schema (column) name to filter (where True),
                         or an Expr to filter the repository listing in `list_repos`.
            tree_filter: Either a Polars schema (column) name to filter (where True),
                         or an Expr to filter the repository tree in `walk_file_trees`.
        """
        self.username = username
        self.lazy = lazy
        self.token = token if token is not None else ENV_GH_TOKEN
        self.use_cache = use_cache
        self.force_refresh = force_refresh
        self._inventory_df: pl.DataFrame | None = None

        # Initialize the cache location
        self._cache_dir = Path(user_cache_dir(appname="reswirl"))
        self._cache_dir.mkdir(exist_ok=True)
        self._cache_file = self._cache_dir / f"{username}_repos.json"

    def list_repos(self) -> pl.DataFrame:
        """
        Fetches and parses the public repositories for the specified GitHub user.
        Checks the local cache first (unless force_refresh=True).
        Returns a Polars DataFrame with columns such as 'name', 'html_url', and 'description'.
        """
        # 1. Retrieve the user’s repos (cached or fresh)
        self._inventory_df = self._retrieve_repos()
        return self._inventory_df

    def review_version_changes(
        self,
        from_v: str = "first",
        to_v: str = "latest",
    ) -> pl.DataFrame:
        """
        Compare repository metadata across two versions (placeholder).
        Currently returns a trivial DataFrame.
        """
        return pl.DataFrame({"from_v": [from_v], "to_v": [to_v]})

    def _retrieve_repos(self) -> pl.DataFrame:
        """
        Tries to use cached results if use_cache=True (and not force_refresh).
        Otherwise, fetches from GitHub and caches the JSON if successful.
        """
        # If using cache and not forcing a refresh, try reading from file
        if self.use_cache and not self.force_refresh:
            cached_data = self._read_cache()
            if cached_data is not None:
                return cached_data

        # Otherwise, fetch from GitHub
        try:
            repos_data = self._fetch_from_github()
            # Cache the data
            self._write_cache(repos_data)
            return repos_data
        except Exception as e:
            # If something goes wrong with GitHub fetching, fallback to cache if it exists
            cached_data = self._read_cache()
            if cached_data is not None:
                print(f"GitHub fetch failed ({e}), returning cached data.")
                return cached_data
            raise  # or handle this more gracefully in real usage

    def _fetch_from_github(self) -> pl.DataFrame:
        """
        Uses PyGithub to retrieve the user's public repositories.
        """
        gh = Github(self.token) if self.token else Github()
        user = gh.get_user(self.username)
        repos = user.get_repos()

        data = [
            {
                "name": repo.name,
                "default_branch": repo.default_branch,
                "description": repo.description or "",
                "archived": repo.archived,
                "is_fork": repo.fork,
                "issues": repo.open_issues,
                "stars": repo.stargazers_count,
                "forks": repo.forks_count,
                "size": repo.size,
            }
            for repo in repos
        ]
        df = pl.DataFrame(data)
        return df.lazy().collect() if self.lazy else df

    def _read_cache(self) -> pl.DataFrame | None:
        """
        Attempt to read previously cached JSON data from disk.
        Returns None if no file or if something fails to load.
        """
        if not self._cache_file.is_file():
            return None
        try:
            return pl.read_ndjson(self._cache_file)
        except OSError:
            return None

    def _write_cache(self, data: pl.DataFrame) -> None:
        """
        Write JSON data to the cache file.
        """
        try:
            data.write_ndjson(self._cache_file)
        except OSError as e:
            print(f"Failed to write to cache: {e}")

    def walk_file_trees(
        self,
        pattern: str = "**",
        no_recurse: bool = False,
        skip_larger_than_mb: int | None = None,
    ) -> pl.DataFrame:
        """
        Walks (recursively enumerates) files in each repository via UPath,
        discovering (but not reading) file paths that match a given glob pattern.

        Args:
            pattern: Glob pattern for file listing. By default "**" (recursive).
            no_recurse: If True, uses "*" (non-recursive) instead of the default "**".
            skip_larger_than_mb: If set, skip listing files larger than this many MB.
                                 By default, None (don't skip based on size).

        Returns:
            A Polars DataFrame with columns:
                - "Repository_Name": str
                - "File_Path": str (the path in the GitHub “filesystem”)
                - "Is_Directory": bool
                - "File_Size_Bytes": int

        Notes:
            - **Slow** for large repos or wide patterns, as it enumerates all matches.
            - If skip_larger_than_mb is set, we call `p.stat().st_size` for each file,
              skipping ones that exceed that threshold.
        """
        # Ensure we have a repo inventory
        if self._inventory_df is None:
            self.list_repos()
        # Adjust pattern if user wants a shallow (non-recursive) listing
        if no_recurse:
            pattern = "*"
        records = []
        for row in self._inventory_df.to_dicts():
            repo_name = row["name"]
            default_branch = row["default_branch"]
            # Construct a GitHub UPath; note org=self.username for personal repos
            ghpath = UPath(
                "/",
                protocol="github",
                org=self.username,
                repo=repo_name,
                sha=default_branch,
                username=self.username,  # used for BasicAuth
                token=self.token,  # personal access token
            )
            for p in ghpath.glob(pattern):
                # Check if directory
                if is_dir := p.is_dir():
                    file_size_bytes = 0
                else:
                    # For skipping large files or just capturing size
                    file_size_bytes = p.stat().st_size
                    if skip_larger_than_mb is not None:
                        threshold_bytes = skip_larger_than_mb * 1_048_576
                        if file_size_bytes > threshold_bytes:
                            # Skip this file
                            continue
                records.append(
                    {
                        "repository_name": repo_name,
                        "file_path": os.path.join(*p.parts),
                        "is_directory": is_dir,
                        "file_size_bytes": file_size_bytes,
                    },
                )
        return pl.DataFrame(records)
