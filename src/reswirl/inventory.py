# src/reswirl/inventory.py

from __future__ import annotations

import os
import polars as pl
from pathlib import Path
from github import Github
from platformdirs import user_cache_dir

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
    ) -> None:
        """
        Initialise the Inventory object.

        Args:
            username: The GitHub username to fetch repositories for.
            lazy: Whether to allow lazy Polars operations (not all transformations may be supported).
            token: An optional GitHub personal access token for higher rate limits.
            use_cache: Whether to use cached results if available.
            force_refresh: If True, always refetch from GitHub and overwrite the cache.
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

    def fetch_inventory(self) -> pl.DataFrame:
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
