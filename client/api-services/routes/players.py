"""
routes/players.py — Player-related endpoints

  GET /api/players            — paginated player list with metrics
  GET /api/player/<player_id> — single player detail
  GET /api/metrics            — summary metric cards
  GET /api/charts/birthyear   — player counts per birth year
  GET /api/filters            — distinct dropdown values
"""

from flask import Blueprint, request, jsonify, current_app
from pymongo.errors import PyMongoError

from db import db, jsonify_mongo
from utils import build_query_string

players_bp = Blueprint("players", __name__)


# ---------------------------------------------------------------------------
# GET /api/players
# ---------------------------------------------------------------------------
# Query params:
#   birthYear     — exact integer e.g. 2010  (legacy, single-year)
#   birthYearFrom — range start  e.g. 2006   (inclusive)
#   birthYearTo   — range end    e.g. 2008   (inclusive)
#   season        — string  e.g. "2025-2026"
#   league        — string  e.g. "ushs-prep"
#   position      — string  "G" | "F/D"
#   search        — string  (matches name or team, case-insensitive)
#   page          — integer (default 0)
#   pageSize      — integer (default 50)
#   sortBy        — field name (default "player_name")
#   sortDir       — "asc" | "desc" (default "asc")
# ---------------------------------------------------------------------------
@players_bp.route("/api/players")
def get_players():
    try:
        # ── Query params ──────────────────────────────────────────────────────
        birth_year      = request.args.get("birthYear")
        birth_year_from = request.args.get("birthYearFrom")
        birth_year_to   = request.args.get("birthYearTo")
        season          = request.args.get("season")
        league          = request.args.get("league")
        position        = request.args.get("position")
        search          = request.args.get("search", "").strip()
        page            = max(0, int(request.args.get("page", 0)))
        page_size       = min(200, max(1, int(request.args.get("pageSize", 50))))
        sort_by         = request.args.get("sortBy", "player_name")
        sort_dir        = 1 if request.args.get("sortDir", "asc") == "asc" else -1

        stats_col = db["stats"]

        # ── 1. Base $match (stats-level fields only, hits indexes early) ──────
        base_match = {}
        if season:   base_match["season"]   = season
        if league:   base_match["league"]   = league
        if position: base_match["position"] = position
        if search:
            base_match["$or"] = [
                {"player_name": {"$regex": search, "$options": "i"}},
                {"team":        {"$regex": search, "$options": "i"}},
            ]

        # ── 2. Lookup + bio fields ────────────────────────────────────────────
        LOOKUP_STAGES = [
            {
                "$lookup": {
                    "from":         "players",
                    "localField":   "player_url",
                    "foreignField": "url",
                    "as":           "bio",
                },
            },
            {"$unwind": {"path": "$bio", "preserveNullAndEmptyArrays": True}},
            {
                "$addFields": {
                    # Extract birth year as int once; reuse everywhere below
                    "birthYearRaw": {
                        "$convert": {
                            "input": {
                                "$substr": [
                                    {"$ifNull": ["$bio.birthDate", "0000-00-00"]}, 0, 4
                                ]
                            },
                            "to":      "int",
                            "onError": 0,
                            "onNull":  0,
                        }
                    },
                    "birthDate":   "$bio.birthDate",
                    "birthPlace":  "$bio.birthPlace",
                    "birthState": {
                        "$ifNull": [
                            {"$getField": {
                                "field": "addressRegion",
                                "input": {"$getField": {
                                    "field": "address",
                                    "input": "$bio.birthPlace",
                                }},
                            }},
                            "",
                        ]
                    },
                    "nationality": "$bio.nationality",
                    "knowsAbout":  "$bio.knowsAbout",
                },
            },
            {
                "$addFields": {
                    "birthYear": {
                        "$cond": {
                            "if":   {"$lte": ["$birthYearRaw", 0]},
                            "then": "N/A",
                            "else": "$birthYearRaw",
                        }
                    }
                }
            },
        ]

        # ── 3. Optional birth-year filter ─────────────────────────────────────
        birth_year_match = {}
        if birth_year_from or birth_year_to:
            try:
                if birth_year_from:
                    birth_year_match["$gte"] = int(birth_year_from)
                if birth_year_to:
                    birth_year_match["$lte"] = int(birth_year_to)
            except ValueError:
                pass
        elif birth_year:
            try:
                birth_year_match = int(birth_year)          # exact int match
            except ValueError:
                if birth_year.upper() == "N/A":
                    birth_year_match = "N/A"                # matched on birthYear string

        # ── 4. Assemble shared pipeline (base match → lookup → birth filter) ──
        pipeline: list = []
        if base_match:
            pipeline.append({"$match": base_match})
        pipeline.extend(LOOKUP_STAGES)
        if birth_year_match:
            field = "birthYear" if birth_year_match == "N/A" else "birthYearRaw"
            pipeline.append({"$match": {field: birth_year_match}})

        # ── 5. Position helper (used inside $group below) ─────────────────────
        _FWD = ["F", "LW", "RW", "C", "FORWARD", "F/D", "F-D"]
        _DEF = ["D", "DEF", "DEFENSE", "DEFENSEMAN", "F/D", "F-D"]
        _GK  = ["G", "GK", "GOALIE"]

        def _pos_upper(field):
            return {"$toUpper": {"$ifNull": [field, ""]}}

        def _is_pos(positions):
            return {"$in": [_pos_upper("$position"), positions]}

        # ── 6. Single $facet: metrics + paginated rows in one round-trip ──────
        #
        #  Metrics use conditional $max / $avg instead of $push-ing every doc,
        #  eliminating the O(n) in-memory array that the original built.
        #
        facet_pipeline = pipeline + [
            {
                "$facet": {
                    # ── 6a. Metrics branch ────────────────────────────────────
                    "metrics": [
                        {
                            "$group": {
                                "_id": None,
                                "totalPlayers": {"$sum": 1},
                                "forwards":   {"$sum": {"$cond": [_is_pos(_FWD), 1, 0]}},
                                "defensemen": {"$sum": {"$cond": [_is_pos(_DEF), 1, 0]}},
                                "goalies":    {"$sum": {"$cond": [_is_pos(_GK),  1, 0]}},
                                "avgGP":  {"$avg": "$stats.gp"},
                                # Only average meaningful (> 0) values
                                "avgGAA": {"$avg": {"$cond": [{"$gt": ["$stats.gaa",    0]}, "$stats.gaa",    "$$REMOVE"]}},
                                "avgSVP": {"$avg": {"$cond": [{"$gt": ["$stats.sv_pct", 0]}, "$stats.sv_pct", "$$REMOVE"]}},
                                "maxPTS": {"$max": "$stats.pts"},
                                "leagues": {"$addToSet": "$league"},
                                # Top forward: track name+pts together via $maxN workaround —
                                # accumulate only forward rows, keep highest pts
                                "fwdMaxPts": {
                                    "$max": {
                                        "$cond": [
                                            _is_pos(_FWD),
                                            "$stats.pts",
                                            "$$REMOVE",
                                        ]
                                    }
                                },
                                "defMaxPts": {
                                    "$max": {
                                        "$cond": [
                                            _is_pos(_DEF),
                                            "$stats.pts",
                                            "$$REMOVE",
                                        ]
                                    }
                                },
                                "gkMaxSVP": {
                                    "$max": {
                                        "$cond": [
                                            _is_pos(_GK),
                                            "$stats.sv_pct",
                                            "$$REMOVE",
                                        ]
                                    }
                                },
                                "avgDefenseBlks": {
                                    "$avg": {
                                        "$cond": [
                                            _is_pos(["D", "DEF"]),
                                            "$stats.blocked_shots",
                                            "$$REMOVE",
                                        ]
                                    }
                                },
                            }
                        },
                        {
                            "$project": {
                                "_id": 0,
                                "totalPlayers": 1,
                                "forwards": 1, "defensemen": 1, "goalies": 1,
                                "avgGP": 1, "avgGAA": 1, "avgSVP": 1,
                                "maxPTS": 1, "avgDefenseBlks": 1,
                                "leagueCount":  {"$size": {"$ifNull": ["$leagues", []]}},
                                "fwdMaxPts": 1, "defMaxPts": 1, "gkMaxSVP": 1,
                            }
                        },
                    ],

                    # ── 6b. Top scorers: tiny secondary lookup for names only ─
                    #   These are three small $sort+$limit passes on an already-
                    #   filtered set — far cheaper than $push on every doc.
                    "topForward": [
                        {"$match": {
                            "$expr": _is_pos(_FWD)
                        }},
                        {"$sort":  {"stats.pts": -1}},
                        {"$limit": 1},
                        {"$project": {"_id": 0, "player_name": 1, "stats.pts": 1}},
                    ],
                    "topDefense": [
                        {"$match": {"$expr": _is_pos(_DEF)}},
                        {"$sort":  {"stats.pts": -1}},
                        {"$limit": 1},
                        {"$project": {"_id": 0, "player_name": 1, "stats.pts": 1}},
                    ],
                    "topGoalie": [
                        {"$match": {"$expr": _is_pos(_GK)}},
                        {"$sort":  {"stats.sv_pct": -1}},
                        {"$limit": 1},
                        {"$project": {
                            "_id": 0,
                            "player_name":   1,
                            "stats.sv_pct":  1,
                            "stats.gaa":     1,
                        }},
                    ],

                    # ── 6c. Paginated data branch ─────────────────────────────
                    "data": [
                        {"$sort":  {sort_by: sort_dir}},
                        {"$skip":  page * page_size},
                        {"$limit": page_size},
                        {"$project": {"bio": 0, "birthYearRaw": 0, "source_player_id": 0}},
                    ],

                    # ── 6d. Cheap total count ─────────────────────────────────
                    "count": [{"$count": "n"}],
                }
            }
        ]

        facet_result = stats_col.aggregate(facet_pipeline, allowDiskUse=True)
        row = next(facet_result, {})

        # ── 7. Unpack facet result ────────────────────────────────────────────
        total        = (row.get("count") or [{}])[0].get("n", 0)
        results      = row.get("data", [])
        m_list       = row.get("metrics") or [{}]
        m            = m_list[0] if m_list else {}

        fwd_doc = (row.get("topForward") or [{}])[0]
        def_doc = (row.get("topDefense") or [{}])[0]
        gk_doc  = (row.get("topGoalie")  or [{}])[0]

        def _svp(raw):
            """Normalise a save-percentage that may be stored as 0–1 or 0–100."""
            v = raw or 0.0
            return round(v * 100.0, 2) if 0.0 < v <= 1.0 else round(v, 2)

        raw_avg_svp = m.get("avgSVP") or 0.0
        gk_stats    = gk_doc.get("stats") or {}

        metrics = {
            "totalPlayers":     total,
            "forwards":         m.get("forwards",     0),
            "defensemen":       m.get("defensemen",   0),
            "goalies":          m.get("goalies",      0),
            "avgGP":            round(m.get("avgGP")  or 0, 1),
            "avgGAA":           round(m.get("avgGAA") or 0, 2),
            "avgSVP":           _svp(raw_avg_svp),
            "maxPTS":           m.get("maxPTS") or 0,
            "leagueCount":      m.get("leagueCount",  0),
            "avgDefenseBlks":   round(m.get("avgDefenseBlks") or 0, 1),
            "topForwardScorer": fwd_doc.get("player_name", "—"),
            "maxForwardPts":    (fwd_doc.get("stats") or {}).get("pts", 0),
            "topDefenseScorer": def_doc.get("player_name", "—"),
            "maxDefensePts":    (def_doc.get("stats") or {}).get("pts", 0),
            "topGoalie":        gk_doc.get("player_name", "—"),
            "maxGoalieSVP":     _svp(gk_stats.get("sv_pct", 0)),
            "topGoalieGAA":     round(gk_stats.get("gaa", 0.0), 2),
        }

        return jsonify_mongo(current_app, {
            "total":    total,
            "page":     page,
            "pageSize": page_size,
            "pages":    (total + page_size - 1) // page_size,
            "data":     results,
            "metrics":  metrics,
        })

    except Exception as e:
        print(f"Error in get_players: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/players/export
# ---------------------------------------------------------------------------
# Same parameters as /api/players but returns ALL matching records (no pagination)
# for CSV export purposes
# ---------------------------------------------------------------------------
@players_bp.route("/api/players/export")
def export_players():
    try:
        birth_year      = request.args.get("birthYear")
        birth_year_from = request.args.get("birthYearFrom")
        birth_year_to   = request.args.get("birthYearTo")
        season          = request.args.get("season")
        league          = request.args.get("league")
        position        = request.args.get("position")
        search          = request.args.get("search", "").strip()
        sort_by         = request.args.get("sortBy", "player_name")
        sort_dir        = 1 if request.args.get("sortDir", "asc") == "asc" else -1

        stats_collection = db["stats"]

        # 1. Base query construction (same as get_players)
        base_match = {}
        if season:   base_match["season"]   = season
        if league:   base_match["league"]   = league
        if position: base_match["position"] = position
        if search:
            base_match["$or"] = [
                {"player_name": {"$regex": search, "$options": "i"}},
                {"team":        {"$regex": search, "$options": "i"}},
            ]

        pipeline = [{"$match": base_match}] if base_match else []

        # 2. Join with players bio (same as get_players)
        pipeline += [
            {
                "$lookup": {
                    "from": "players",
                    "localField": "player_url",
                    "foreignField": "url",
                    "as": "bio"
                }
            },
            {"$unwind": {"path": "$bio", "preserveNullAndEmptyArrays": True}},
            {
                "$addFields": {
                    "birthYearRaw": {
                        "$toInt": {
                            "$substr": [
                                {"$ifNull": ["$bio.birthDate", "0000-00-00"]}, 0, 4
                            ]
                        }
                    },
                    "birthDate":   "$bio.birthDate",
                    "birthPlace":  "$bio.birthPlace",
                    "birthState":  {
                        "$ifNull": [
                            {"$getField": {
                                "field": "addressRegion",
                                "input": {"$getField": {
                                    "field": "address",
                                    "input": "$bio.birthPlace"
                                }}
                            }},
                            ""
                        ]
                    },
                    "nationality": "$bio.nationality",
                    "knowsAbout":  "$bio.knowsAbout",
                }
            },
            {
                "$addFields": {
                    "birthYear": {
                        "$cond": {
                            "if": {"$or": [
                                {"$eq": ["$birthYearRaw", 0]},
                                {"$eq": ["$birthYearRaw", None]}
                            ]},
                            "then": "N/A",
                            "else": "$birthYearRaw"
                        }
                    }
                }
            }
        ]

        # 3. Birth year post-filter
        by_filter = {}
        if birth_year_from or birth_year_to:
            try:
                if birth_year_from:
                    by_filter["$gte"] = int(birth_year_from)
                if birth_year_to:
                    by_filter["$lte"] = int(birth_year_to)
                pipeline.append({"$match": {"birthYearRaw": by_filter}})
            except ValueError:
                pass
        elif birth_year:
            try:
                pipeline.append({"$match": {"birthYearRaw": int(birth_year)}})
            except ValueError:
                if birth_year.upper() == "N/A":
                    pipeline.append({"$match": {"birthYear": "N/A"}})

        # 4. Get all matching records without pagination
        pipeline += [
            {"$sort": {sort_by: sort_dir}},
            {"$project": {"bio": 0, "birthYearRaw": 0, "source_player_id": 0}}
        ]
        results = list(stats_collection.aggregate(pipeline))

        return jsonify_mongo(current_app, {
            "data": results
        })

    except Exception as e:
        print(f"Error in export_players: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/player/<player_id>
# ---------------------------------------------------------------------------
@players_bp.route("/api/player/<player_id>")
def get_player(player_id):
    try:
        bio = db.players.find_one(
            {"url": {"$regex": f"/player/{player_id}/"}},
            {"_id": 0}
        )
        stats = list(
            db.stats.find(
                {"player_id": player_id},
                {"_id": 0, "source_player_id": 0, "source_team_id": 0}
            ).sort("season", -1)
        )
        return jsonify_mongo(current_app, {"bio": bio, "stats": stats})
    except PyMongoError as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/metrics
# ---------------------------------------------------------------------------
@players_bp.route("/api/metrics")
def get_metrics():
    birth_year = request.args.get("birthYear")
    season     = request.args.get("season")
    league     = request.args.get("league")
    position   = request.args.get("position")

    match = {}
    if birth_year:
        try:
            match["birthYear"] = int(birth_year)
        except ValueError:
            pass
    if season:   match["season"]   = season
    if league:   match["league"]   = league
    if position: match["position"] = position

    pipeline = [
        {"$lookup": {
            "from": "players",
            "localField": "player_url",
            "foreignField": "url",
            "as": "bio"
        }},
        {"$unwind": {"path": "$bio", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {
            "birthYear": {
                "$toInt": {"$substr": [{"$ifNull": ["$bio.birthDate", "0000"]}, 0, 4]}
            }
        }},
    ]
    if match:
        pipeline.append({"$match": match})

    pipeline += [{
        "$group": {
            "_id":          None,
            "totalPlayers": {"$sum": 1},
            "goalies":      {"$sum": {"$cond": [{"$eq": ["$position", "G"]}, 1, 0]}},
            "skaters":      {"$sum": {"$cond": [{"$ne": ["$position", "G"]}, 1, 0]}},
            "avgGP":        {"$avg": "$stats.gp"},
            "avgGAA":       {"$avg": "$stats.gaa"},
            "avgSVP":       {"$avg": "$stats.sv_pct"},
            "maxPTS":       {"$max": "$stats.pts"},
            "leagues":      {"$addToSet": "$league"},
            "seasons":      {"$addToSet": "$season"},
        }
    }]

    try:
        result = list(db.stats.aggregate(pipeline))
        if result:
            r = result[0]
            r.pop("_id", None)
            r["leagueCount"] = len(r.pop("leagues", []))
            r["seasonCount"]  = len(r.pop("seasons",  []))
            return jsonify_mongo(current_app, r)
        return jsonify({})
    except PyMongoError as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/charts/birthyear
# ---------------------------------------------------------------------------
@players_bp.route("/api/charts/birthyear")
def chart_birthyear():
    pipeline = [
        {"$lookup": {
            "from": "players",
            "localField": "player_url",
            "foreignField": "url",
            "as": "bio"
        }},
        {"$unwind": {"path": "$bio", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {
            "birthYear": {
                "$toInt": {"$substr": [{"$ifNull": ["$bio.birthDate", "0000"]}, 0, 4]}
            }
        }},
        {"$group": {"_id": "$birthYear", "count": {"$sum": 1}}},
        {"$match": {"_id": {"$gt": 1990}}},
        {"$sort":  {"_id": 1}},
        {"$project": {"year": "$_id", "count": 1, "_id": 0}}
    ]
    try:
        return jsonify_mongo(current_app, list(db.stats.aggregate(pipeline)))
    except PyMongoError as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/league/team-roster-origins
# ---------------------------------------------------------------------------
# Query params:
#   league  — required, e.g. "ushs-prep"
#   season  — optional, e.g. "2024-2025"
#
# Returns an array of teams, each with:
#   { team, player_count, players: [{ name, position, birth_place,
#                                     youth_team, youth_league, youth_season }] }
#
# "youth_team" is the earliest team in the DB that is *different* from the
# player's current team — i.e. where they played before joining this roster.
# If a player has always been on the same team, youth_team is null.
# ---------------------------------------------------------------------------
@players_bp.route("/api/league/team-roster-origins")
def team_roster_origins():
    league = request.args.get("league")
    season = request.args.get("season")

    if not league:
        return jsonify({"error": "league param required"}), 400

    match = {"league": league}
    if season:
        match["season"] = season

    pipeline = [
        # 1. Scope to current league / season
        {"$match": match},

        # 2. One row per (team, player) — eliminates duplicates if multiple
        #    stat records exist for the same player in the same league
        {"$group": {
            "_id": {"team": "$team", "player_url": "$player_url"},
            "player_name": {"$first": "$player_name"},
            "position":    {"$first": "$position"},
        }},

        # 3. Join with players bio for birth-place data
        {"$lookup": {
            "from":         "players",
            "localField":   "_id.player_url",
            "foreignField": "url",
            "as":           "bio"
        }},
        {"$unwind": {"path": "$bio", "preserveNullAndEmptyArrays": True}},

        # 4. Find earliest recorded stats on a DIFFERENT team
        #    → that's the player's youth / prior team
        {"$lookup": {
            "from": "stats",
            "let":  {"purl": "$_id.player_url", "curr": "$_id.team"},
            "pipeline": [
                {"$match": {"$expr": {
                    "$and": [
                        {"$eq":  ["$player_url", "$$purl"]},
                        {"$ne":  ["$team",       "$$curr"]},
                    ]
                }}},
                {"$sort":    {"season": 1}},
                {"$limit":   1},
                {"$project": {"_id": 0, "team": 1, "league": 1, "season": 1}},
            ],
            "as": "prior_stat"
        }},

        # 5. Shape each player document
        {"$project": {
            "_id":         0,
            "team":        "$_id.team",
            "player_url":  "$_id.player_url",
            "player_name": 1,
            "position":    1,
            "birth_place": "$bio.birthPlace",
            "birth_date":  "$bio.birthDate",
            "youth_team":   {"$arrayElemAt": ["$prior_stat.team",   0]},
            "youth_league": {"$arrayElemAt": ["$prior_stat.league", 0]},
            "youth_season": {"$arrayElemAt": ["$prior_stat.season", 0]},
        }},

        # 6. Group by current team
        {"$group": {
            "_id":          "$team",
            "player_count": {"$sum": 1},
            "players":      {"$push": {
                "name":         "$player_name",
                "position":     "$position",
                "player_url":   "$player_url",
                "birth_place":  "$birth_place",
                "birth_date":   "$birth_date",
                "youth_team":   "$youth_team",
                "youth_league": "$youth_league",
                "youth_season": "$youth_season",
            }},
        }},

        # 7. Sort alphabetically by team name
        {"$sort": {"_id": 1}},

        {"$project": {
            "_id":          0,
            "team":         "$_id",
            "player_count": 1,
            "players":      1,
        }},
    ]

    try:
        result = list(db.stats.aggregate(pipeline))
        return jsonify_mongo(current_app, result)
    except Exception as exc:
        print(f"Error in team_roster_origins: {exc}")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# GET /api/filters
# ---------------------------------------------------------------------------
@players_bp.route("/api/filters")
def get_filters():
    try:
        birth_years = sorted([
            int(y[:4]) for y in db.players.distinct("birthDate")
            if y and len(y) >= 4 and y[:4].isdigit()
        ], reverse=True)

        seasons   = sorted(db.stats.distinct("season"),   reverse=True)
        leagues   = sorted(db.stats.distinct("league"))
        positions = sorted(db.stats.distinct("position"))

        return jsonify({
            "birthYears": birth_years,
            "seasons":    seasons,
            "leagues":    leagues,
            "positions":  positions,
        })
    except PyMongoError as e:
        return jsonify({"error": str(e)}), 500
    
def get_departing_players_pipeline(birth_years=None, max_year=None):
    """
    Generates the aggregation pipeline linking players and teams.

    Args:
        birth_years: list of specific years e.g. [2007, 2008]  — takes priority
        max_year:    upper bound e.g. 2008  — used when no specific years given
                     defaults to 2007 if neither param is provided
    """

    # ── Build the birth-year $match expression ───────────────────────────────
    if birth_years and len(birth_years) > 0:
        # Exact multi-year match: year IN [2006, 2007, 2008]
        year_match = {
            "$expr": {
                "$in": [
                    {
                        "$convert": {
                            "input": {"$substrCP": ["$birthDate", 0, 4]},
                            "to": "int",
                            "onError": 9999,
                            "onNull": 9999
                        }
                    },
                    birth_years
                ]
            }
        }
    else:
        # Range match: year <= max_year (default 2007)
        cutoff = max_year if max_year is not None else 2007
        year_match = {
            "$expr": {
                "$lte": [
                    {
                        "$convert": {
                            "input": {"$substrCP": ["$birthDate", 0, 4]},
                            "to": "int",
                            "onError": 9999,
                            "onNull": 9999
                        }
                    },
                    cutoff
                ]
            }
        }

    return [
        # 1. Filter out missing or corrupt birth dates up front
        {
            "$match": {
                "birthDate": {"$ne": None, "$not": {"$type": "string", "$eq": ""}}
            }
        },
        # 2. Dynamic birth year filter
        {"$match": year_match},
        # 3. Join players collection with the teams collection
        {
            "$lookup": {
                "from": "teams",
                "localField": "source_team_id",
                "foreignField": "_id",
                "as": "teamDetails"
            }
        },
        # 4. Flatten the team details array
        {"$unwind": "$teamDetails"},
        # 5. Restrict strictly to USHS-Prep league and Ice Hockey
        {
            "$match": {
                "teamDetails.sport": "Ice Hockey",
                "$or": [
                    {"teamDetails.memberOf.name": "USHS-Prep"},
                    {"teamDetails.memberOf.id": "USHS-Prep"},
                    {"teamDetails.memberOf": {"$regex": "USHS-Prep", "$options": "i"}}
                ]
            }
        },
        # 6. Group by school and compile metrics/lists
        {
            "$group": {
                "_id": "$source_team_id",
                "schoolName": {"$first": "$teamDetails.name"},
                "schoolLogo": {"$first": "$teamDetails.image"},
                "coachingStaff": {"$first": "$teamDetails.coach"},
                "eliteProspectsTeamUrl": {"$first": "$teamDetails.mainEntityOfPage"},
                "departingPlayersCount": {"$sum": 1},
                "leavingPlayersList": {
                    "$push": {
                        "name": "$name",
                        "birthDate": "$birthDate",
                        "url": "$url"
                    }
                }
            }
        },
        # 7. Discard teams with unrealistic numbers (> 15 departures)
        {
            "$match": {
                "departingPlayersCount": {"$lte": 15}
            }
        },
        # 8. Sort by schools with the highest vacancies first
        {"$sort": {"departingPlayersCount": -1}}
    ]


@players_bp.route('/api/recruitment/vacancies', methods=['GET'])
def get_vacancies():
    """
    API Endpoint returning USHS-Prep schools losing players.

    Query params:
        birthYears  comma-separated list of exact years e.g. ?birthYears=2007,2008
        maxYear     upper-bound year              e.g. ?maxYear=2008
        (if neither provided, defaults to maxYear=2007)

    Examples:
        /api/recruitment/vacancies                       → born <= 2007
        /api/recruitment/vacancies?maxYear=2008          → born <= 2008
        /api/recruitment/vacancies?birthYears=2007,2008  → born exactly 2007 or 2008
        /api/recruitment/vacancies?birthYears=2006       → born exactly 2006 only
    """
    try:
        # Parse birthYears param — comma-separated string → list of ints
        birth_years_raw = request.args.get("birthYears", "").strip()
        birth_years = []
        if birth_years_raw:
            try:
                birth_years = [int(y.strip()) for y in birth_years_raw.split(",") if y.strip()]
            except ValueError:
                return jsonify({"status": "error", "message": "birthYears must be comma-separated integers"}), 400

        # Parse maxYear param
        max_year = None
        max_year_raw = request.args.get("maxYear", "").strip()
        if max_year_raw:
            try:
                max_year = int(max_year_raw)
            except ValueError:
                return jsonify({"status": "error", "message": "maxYear must be an integer"}), 400

        pipeline = get_departing_players_pipeline(
            birth_years=birth_years if birth_years else None,
            max_year=max_year
        )
        results = list(db.players.aggregate(pipeline))

        for record in results:
            record["_id"] = str(record["_id"])

        return jsonify({
            "status": "success",
            "count": len(results),
            "birthYears": birth_years or None,
            "maxYear": max_year,
            "data": results
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/schools/recruiting
# ---------------------------------------------------------------------------
# Find schools actively recruiting specific birth year cohort
# Query params:
#   birthYear      — integer e.g. 2011 (target birth year to recruit, REQUIRED)
#   season         — optional, e.g. "2025-2026" (default: all seasons)
#   league         — optional, e.g. "ushs-prep" (filters schools by league)
#   agingOutYears  — optional, default from config (AGING_OUT_YEARS_THRESHOLD env var, default 3)
#                    minimum age for aging out is 18 years old
#
# Returns: Array of schools with player composition analysis
# {
#   team: "School Name",
#   league: "ushs-prep",
#   season: "2025-2026",
#   players_target_cohort: 5,          # Count of target birth year players (2011-born)
#   players_aging_out: 12,             # Count of 18+ year old players (born 2008 or earlier in 2026)
#   players_future_talent: 3,          # Count of younger players
#   total_players: 20,
#   forwards/defensemen/goalies: {...},
#   aging_out_percentage: 60.0,
#   recruitment_priority: "HIGH"       # Based on aging_out % (>40% HIGH, >20% MEDIUM, else LOW)
# }
# ---------------------------------------------------------------------------
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app

# Assuming db and jsonify_mongo are imported/defined elsewhere in your project
# from your_app.extensions import db, jsonify_mongo 

@players_bp.route("/api/schools/recruiting")
def find_recruiting_schools():
    try:
        birth_year = request.args.get("birthYear")
        season = request.args.get("season")
        league = request.args.get("league")
        aging_out_years_str = request.args.get("agingOutYears")
        
        # Dynamically get current year and apply prep school age limit (18)
        current_year = datetime.now().year
        max_age_limit = 18

        if not birth_year:
            return jsonify({"error": "birthYear parameter required"}), 400

        try:
            target_year = int(birth_year)
        except ValueError:
            return jsonify({"error": f"Invalid birthYear: {birth_year}"}), 400

        # Get aging out years threshold from query param or config (default: 3)
        aging_out_years = 3  # Default fallback
        try:
            if aging_out_years_str:
                aging_out_years = int(aging_out_years_str)
            else:
                try:
                    from common.config import Config
                    aging_out_years = Config().aging_out_years_threshold
                except (ImportError, Exception):
                    pass  # Use default of 3
        except (ValueError, TypeError):
            pass  # Use default of 3
        
        # Calculate the dynamic aging-out birth year cutoff formula
        aging_out_birth_year_cutoff = current_year - max(aging_out_years, max_age_limit)
        print(f"Calculated aging out cutoff birth year: {aging_out_birth_year_cutoff} (current_year={current_year}, max_age_limit={max_age_limit}, aging_out_years={aging_out_years})")
        
        stats_collection = db["stats"]

        # Build initial match filter
        match_filter = {}
        if season:
            match_filter["season"] = season
        if league:
            match_filter["league"] = league

        pipeline = [
            # 1. Filter stats by season/league if provided
            {"$match": match_filter} if match_filter else {"$match": {}},
            
            # 2. Join with players collection to fetch bio data
            {
                "$lookup": {
                    "from": "players",
                    "localField": "player_url",
                    "foreignField": "url",
                    "as": "bio"
                }
            },
            {"$unwind": {"path": "$bio", "preserveNullAndEmptyArrays": True}},
            
            # 3. Parse birth year safely from the date string
            {
                "$addFields": {
                    "birthYearRaw": {
                        "$toInt": {
                            "$substr": [
                                {"$ifNull": ["$bio.birthDate", "0000-00-00"]}, 0, 4
                            ]
                        }
                    },
                    "playerPosition": {
                        "$toUpper": {"$ifNull": ["$position", ""]}
                    }
                }
            },
            
            # 4. Group by school and compute player metrics
            {
                "$group": {
                    "_id": {
                        "team": "$team",
                        "league": "$league",
                        "season": "$season"
                    },
                    "target_cohort": {
                        "$sum": {"$cond": [{"$eq": ["$birthYearRaw", target_year]}, 1, 0]}
                    },
                    "aging_out": {
                        "$sum": {"$cond": [
                            {"$and": [
                                {"$lte": ["$birthYearRaw", aging_out_birth_year_cutoff]},
                                {"$gt": ["$birthYearRaw", 1900]} # Skips corrupted/empty '0000' records
                            ]}, 
                            1, 0
                        ]}
                    },
                    "future_talent": {
                        "$sum": {"$cond": [{"$gt": ["$birthYearRaw", target_year]}, 1, 0]}
                    },
                    "total_players": {"$sum": 1},
                    "forwards": {
                        "$sum": {"$cond": [{"$in": ["$playerPosition", ["F", "LW", "RW", "C"]]}, 1, 0]}
                    },
                    "defensemen": {
                        "$sum": {"$cond": [{"$in": ["$playerPosition", ["D"]]}, 1, 0]}
                    },
                    "goalies": {
                        "$sum": {"$cond": [{"$in": ["$playerPosition", ["G", "GK"]]}, 1, 0]}
                    }
                }
            },
            
            # 5. Filter for schools that have active target cohort players
            {
                "$match": {"target_cohort": {"$gt": 0}}
            },
            
            # 6. Calculate relative dynamic recruitment priorities
            {
                "$addFields": {
                    "aging_out_percentage": {
                        "$cond": [
                            {"$eq": ["$total_players", 0]},
                            0,
                            {"$multiply": [
                                {"$divide": ["$aging_out", "$total_players"]},
                                100
                            ]}
                        ]
                    },
                    "recruitment_priority": {
                        "$cond": [
                            {"$gte": [
                                {"$divide": ["$aging_out", {"$max": ["$total_players", 1]}]},
                                0.4
                            ]},
                            "HIGH",
                            {"$cond": [
                                {"$gte": [
                                    {"$divide": ["$aging_out", {"$max": ["$total_players", 1]}]},
                                    0.2
                                ]},
                                "MEDIUM",
                                "LOW"
                            ]}
                        ]
                    }
                }
            },
            
            # 7. FIX: Exclude schools where aging_out equals total_players (100% aging out)
            {
                "$match": {
                    "aging_out_percentage": {"$lte": 80}
                }
            },
            
            # 8. Sort by schools losing the most players first
            {"$sort": {"aging_out": -1, "target_cohort": -1}},
            
            # 9. Shape output structure
            {
                "$project": {
                    "_id": 0,
                    "team": "$_id.team",
                    "league": "$_id.league",
                    "season": "$_id.season",
                    "players_target_cohort": "$target_cohort",
                    "players_aging_out": "$aging_out",
                    "players_future_talent": "$future_talent",
                    "total_players": 1,
                    "forwards": 1,
                    "defensemen": 1,
                    "goalies": 1,
                    "aging_out_percentage": {"$round": ["$aging_out_percentage", 1]},
                    "recruitment_priority": 1
                }
            }
        ]

        results = list(stats_collection.aggregate(pipeline))

        return jsonify_mongo(current_app, {
            "target_birth_year": target_year,
            "aging_out_cutoff_year": aging_out_birth_year_cutoff,
            "current_system_year": current_year,
            "filters": {
                "season": season or "all",
                "league": league or "all"
            },
            "schools": results,
            "total_schools": len(results)
        })

    except Exception as e:
        print(f"Error in find_recruiting_schools: {e}")
        return jsonify({"error": str(e)}), 500



# ---------------------------------------------------------------------------
# GET /api/schools/recruiting/aging-out-players
# ---------------------------------------------------------------------------
# Get detailed list of 18+ year old players (aging out) from a specific school
# Query params:
#   school (required) — school/team name
#   season (optional) — filter by season
#   league (optional) — filter by league
#   agingOutYears (optional) — years threshold (default from config, minimum age is always 18)
#
# Returns: List of aging out players (18+) with their details
# ---------------------------------------------------------------------------
@players_bp.route("/api/schools/recruiting/aging-out-players")
def get_aging_out_players():
    try:
        school_name = request.args.get("school")
        season = request.args.get("season")
        league = request.args.get("league")
        aging_out_years_str = request.args.get("agingOutYears")

        if not school_name:
            return jsonify({"error": "school parameter required"}), 400

        # Get aging out years threshold (default: 3)
        aging_out_years = 3
        try:
            if aging_out_years_str:
                aging_out_years = int(aging_out_years_str)
            else:
                try:
                    from common.config import Config
                    aging_out_years = Config().aging_out_years_threshold
                except (ImportError, Exception):
                    pass
        except (ValueError, TypeError):
            pass

        stats_collection = db["stats"]

        # Calculate cutoff: Aging out must be 18+ years old
        current_year = 2026
        min_age_for_aging_out = 18
        target_year_cutoff = current_year - max(aging_out_years, min_age_for_aging_out)

        # Build match filter
        match_filter = {
            "team": school_name
        }
        if season:
            match_filter["season"] = season
        if league:
            match_filter["league"] = league

        current_year = 2026  # Current year
        # Aging out must be 18+ years old
        # If aging_out_years is set, use it, but ensure minimum of 18 years old
        min_age_for_aging_out = 18
        target_year_cutoff = current_year - max(aging_out_years, min_age_for_aging_out)
       
        pipeline = [
            {"$match": match_filter},
            {
                "$lookup": {
                    "from": "players",
                    "localField": "player_url",
                    "foreignField": "url",
                    "as": "bio"
                }
            },
            {"$unwind": {"path": "$bio", "preserveNullAndEmptyArrays": True}},
            {
                "$addFields": {
                    "birthYearRaw": {
                        "$cond": {
                            "if": {"$ifNull": ["$bio.birthDate", None]},
                            "then": {
                                "$year": {
                                    "$dateFromString": {
                                        "dateString": "$bio.birthDate",
                                        "onError": None
                                    }
                                }
                            },
                            "else": None
                        }
                    }
                }
            },
            {
                "$match": {
                    "birthYearRaw": {"$lte": target_year_cutoff}
                }
            },
            {
                "$project": {
                    "player_name": 1,
                    "player_url": 1,
                    "playerPosition": 1,
                    "birthYearRaw": 1,
                    "season": 1,
                    "league": 1,
                    "team": 1,
                    "stats": 1
                }
            },
            {
                "$sort": {"birthYearRaw": 1}  # Oldest first
            }
        ]

        players = list(stats_collection.aggregate(pipeline))

        # Format response
        formatted_players = []
        for p in players:
            stats_obj = p.get("stats") or {}
            gp = stats_obj.get("GP") or stats_obj.get("gp") or 0
            g = stats_obj.get("G") or stats_obj.get("g") or 0
            a_count = stats_obj.get("A") or stats_obj.get("a") or 0
            pts = stats_obj.get("PTS") or stats_obj.get("pts") or 0
            
            if pts == 0 and (g > 0 or a_count > 0):
                pts = g + a_count

            formatted_players.append({
                "name": p.get("player_name", "—"),
                "url": p.get("player_url", ""),
                "position": (p.get("playerPosition") or "—").upper(),
                "birth_year": p.get("birthYearRaw") or "—",
                "age": (2026 - p.get("birthYearRaw")) if p.get("birthYearRaw") else "—",
                "season": p.get("season", "—"),
                "stats": {
                    "GP": gp,
                    "G": g,
                    "A": a_count,
                    "PTS": pts
                }
            })

        return jsonify_mongo(current_app, {
            "school": school_name,
            "aging_out_years": aging_out_years,
            "player_count": len(formatted_players),
            "players": formatted_players
        })

    except Exception as e:
        print(f"Error in get_aging_out_players: {e}")
        return jsonify({"error": str(e)}), 500

