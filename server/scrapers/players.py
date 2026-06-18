"""
server/scrapers/players.py
-------------------------------
PlayerScraper
  Aggregates all athlete URLs from the `teams` collection,
  fetches each player's profile page, extracts the Person JSON-LD,
  and upserts into the `players` collection.

  Source  : teams collection  (athlete[].url)
  Target  : players collection
"""

from __future__ import annotations

import json

from bs4 import BeautifulSoup

from common.config import settings
from server.db.mongo import MongoRepository
from server.models.player import PlayerDocument
from server.scrapers.base import BaseScraper


class TeamsRepository(MongoRepository):
    collection_name = settings.teams_collection


class PlayersRepository(MongoRepository):
    collection_name = settings.players_collection


class PlayerScraper(BaseScraper):
    """
    Iterates every athlete URL found in teams and populates the players collection.

    Usage::

        scraper = PlayerScraper()
        total   = scraper.run()
    """

    def __init__(self) -> None:
        super().__init__(repo=PlayersRepository())
        self._teams_repo = TeamsRepository()

    # ------------------------------------------------------------------

    def run(self) -> int:
        with self._teams_repo:
            athletes = self._collect_athletes()

        self.log.info(
            "Found %d total athlete links across all teams.", len(athletes)
        )

        upserted = 0
        with self.repo:
            with self.http:
                for athlete in athletes:
                    count = self._process_athlete(athlete)
                    upserted += count

        self.log.info("PlayerScraper finished. Upserted %d player records.", upserted)
        return upserted

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _collect_athletes(self) -> list[dict]:
        """
        Aggregate athlete list from all team documents.
        Returns: [{ player_url, player_name, team_id, team_year }, ...]
        """
        pipeline = [
            {"$match": {"athlete": {"$exists": True}}},
            {"$unwind": "$athlete"},
            {
                "$project": {
                    "player_url":  "$athlete.url",
                    "player_name": "$athlete.name",
                    "team_id":     "$_id",
                    "team_year":   "$year",
                }
            },
        ]
        return self._teams_repo.aggregate(pipeline)

    def _process_athlete(self, athlete: dict) -> int:
        url       = athlete.get("player_url", "")
        name      = athlete.get("player_name", "Unknown")
        team_id   = athlete.get("team_id")
        team_year = athlete.get("team_year")

        if not url:
            return 0

        # Skip if already stored for this season
        if self.repo.find_one({"url": url, "year": team_year}):
            self.log.debug("Already stored: %s (%s) — skip.", name, team_year)
            return 0

        self.log.info("Fetching player: %s  %s", name, url)
        html = self.http.get(url)
        self.http.polite_sleep()

        if not html:
            self.log.warning("No response for %s.", name)
            return 0

        person_data = self._parse_person(html)
        if not person_data:
            self.log.warning("No Person JSON-LD for %s.", name)
            return 0

        player_doc = PlayerDocument.from_jsonld(person_data, team_year, team_id)
        # Convert to dictionary and remove the 'year' field if it exists
        player_dict = player_doc.to_dict()
        player_dict.pop('year', None)  # 'None' prevents errors if 'year' isn't in the dict
        # Save the cleaned dictionary
        self.repo.upsert(player_doc.upsert_filter, player_dict)
        self.log.info("Saved player: %s", name)
        return 1

    @staticmethod
    def _parse_person(html: str) -> dict | None:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(
            "script",
            attrs={"type": "application/ld+json", "data-next-head": ""},
        ):
            if not tag.string:
                continue
            try:
                data = json.loads(tag.string)
                if data.get("@type") == "Person":
                    return data
            except json.JSONDecodeError:
                continue
        return None
