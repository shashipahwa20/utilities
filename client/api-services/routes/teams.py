"""
routes/teams.py — Team-related endpoints

  GET /api/teams           — populated team rosters with inline stats
  GET /api/filters/teams   — distinct team/league values for dropdowns
"""

import re
from flask import Blueprint, request, jsonify

from db import db
from utils import safe_int, get_array_or_dict_val

teams_bp = Blueprint("teams", __name__)

SV_KEYS = ("GVSV%", "SV%", "sv%", "sv_pct")


# ---------------------------------------------------------------------------
# GET /api/teams
# ---------------------------------------------------------------------------
# --------------------------------------------------------------------------- #
# GET /api/teams
# --------------------------------------------------------------------------- #
@teams_bp.route("/api/teams", methods=["GET"])
def get_teams_roster():
    try:
        selected_team = request.args.get("search", "").strip()
        athlete_search = request.args.get("athlete", "").strip()
        coach_search = request.args.get("coach", "").strip()
        selected_league = request.args.get("league", "").strip()
        # 🔑 Extract the selected season parameter from the request
        selected_season = request.args.get("season", "").strip()

        # 1. Base query construction
        query = {}
        
        if selected_team and selected_team.lower() != "all teams":
            query["name"] = re.compile(rf".*{re.escape(selected_team)}.*", re.IGNORECASE)
            
        if selected_league and selected_league.lower() != "all leagues":
            query["memberOf.name"] = re.compile(
                rf".*{re.escape(selected_league)}.*", re.IGNORECASE
            )

        # 🔑 Filter query by year if a specific season is selected
        if selected_season and selected_season.lower() != "all seasons":
            try:
                # Convert string param to integer to match your DB schema
                query["year"] = int(selected_season)
            except ValueError:
                # Fallback just in case it is stored as a string structure
                query["year"] = selected_season

        matching_teams = list(db.teams.find(query))

        # 2. Extract all player IDs for batch fetching
        all_player_ids: set = set()
        for record in matching_teams:
            for a in record.get("athlete", []):
                if isinstance(a, dict) and "url" in a:
                    m = re.search(r"/player/(\d+)", str(a["url"]))
                    if m:
                        pid = m.group(1)
                        all_player_ids.add(pid)
                        all_player_ids.add(int(pid)) # both str and int formats

        # 3. Bulk-fetch stats (eliminates N+1 queries)
        stats_map: dict = {}
        if all_player_ids:
            bulk_stats = db.stats.find({
                "player_id": {"$in": list(all_player_ids)},
                "stat_type": "REGULAR_SEASON"
            })
            for stat in bulk_stats:
                pid = stat.get("player_id")
                stats_map[str(pid)] = stat
                if isinstance(pid, (int, float)):
                    stats_map[int(pid)] = stat

        results = []

        # 4. Process teams with in-memory stat lookups
        for record in matching_teams:
            raw_athletes = record.get("athlete", [])
            raw_coaches = record.get("coach", [])
            team_year = record.get("year", 2026)
            
            coaches = [
                str(c.get("name", "")).strip() if isinstance(c, dict) else str(c).strip()
                for c in raw_coaches
            ]
            coaches = [c for c in coaches if c]
            if coach_search:
                coaches = [c for c in coaches if coach_search.lower() in c.lower()]
            if not coaches:
                continue

            athletes_payload = []
            goalie_count = skater_count = 0
            total_games = total_goals = total_assists = scored_records_count = 0
            top_scorer_name = top_assister_name = top_goalie_name = "—"
            max_points = max_assists = -1
            max_sv_pct = -1.0

            for a in raw_athletes:
                if not a:
                    continue
                name = str(a.get("name", "")).strip() if isinstance(a, dict) else str(a).strip()
                if athlete_search and athlete_search.lower() not in name.lower():
                    continue

                player_id_str = ""
                if isinstance(a, dict) and "url" in a:
                    m = re.search(r"/player/(\d+)", str(a["url"]))
                    if m:
                        player_id_str = m.group(1)

                position_tag = "—"
                player_summary = "No stats loaded"

                # O(1) in-memory lookup
                stat_record = None
                if player_id_str:
                    stat_record = (
                        stats_map.get(player_id_str) or 
                        stats_map.get(safe_int(player_id_str))
                    )

                if stat_record:
                    position_tag = stat_record.get("position", "—").strip().upper()
                    is_goalie = position_tag in ("G", "GK", "GOALIE")
                    if is_goalie:
                        goalie_count += 1
                    else:
                        skater_count += 1

                    stats_obj = stat_record.get("stats") or {}
                    gp = safe_int(stats_obj.get("GP") or stats_obj.get("gp"))
                    g = safe_int(stats_obj.get("G") or stats_obj.get("g"))
                    a_count = safe_int(stats_obj.get("A") or stats_obj.get("a"))
                    pts = safe_int(stats_obj.get("PTS") or stats_obj.get("pts"))

                    if pts == 0 and (g > 0 or a_count > 0):
                        pts = g + a_count

                    total_games += gp
                    total_goals += g
                    total_assists += a_count
                    if gp > 0:
                        scored_records_count += 1

                    if is_goalie:
                        sv_raw = next((stats_obj[k] for k in SV_KEYS if k in stats_obj), 0)
                        gaa_raw = get_array_or_dict_val(stats_obj, ("GAA", "gaa"), "—")
                        so_raw = get_array_or_dict_val(stats_obj, ("SO", "so"), 0)
                        toi_raw = get_array_or_dict_val(stats_obj, ("TOI", "toi"), "—")
                        w_raw = get_array_or_dict_val(stats_obj, ("W", "w"), 0)
                        l_raw = get_array_or_dict_val(stats_obj, ("L", "l"), 0)
                        ot_raw = (
                            stats_obj.get("OT") or stats_obj.get("ot")
                            if isinstance(stats_obj, dict) else None
                        )
                        if ot_raw is not None and str(ot_raw).strip().lower() != "null":
                            record_str = f"{w_raw}-{l_raw}-{ot_raw}"
                        else:
                            record_str = f"{w_raw}-{l_raw}"

                        try:
                            sv_str = str(sv_raw).replace("%", "").strip()
                            sv_float = float(sv_str) if sv_str else 0.0
                            comp = sv_float * 100.0 if 0.0 < sv_float <= 1.0 else sv_float
                            if comp > max_sv_pct and comp > 0:
                                max_sv_pct = comp
                                top_goalie_name = f"{name} ({sv_raw})"
                        except (ValueError, TypeError):
                            pass

                        player_summary = (
                            f"GP: {gp} | REC: {record_str} | GAA: {gaa_raw} "
                            f"| SV%: {sv_raw} | SO: {so_raw} | TOI: {toi_raw}"
                        )
                    else:
                        if pts > max_points:
                            max_points = pts
                            top_scorer_name = f"{name} ({pts} PTS)"
                        if a_count > max_assists:
                            max_assists = a_count
                            top_assister_name = f"{name} ({a_count} A)"
                        player_summary = f"GP: {gp} | G: {g} | A: {a_count} | PTS: {pts}"

                athletes_payload.append({
                    "name": name,
                    "position": position_tag,
                    "summary": player_summary
                })

            avg_gp = (
                round(total_games / scored_records_count, 1) 
                if scored_records_count > 0 else "—"
            )
            
            member_of = record.get("memberOf")
            results.append({
                "id": str(record.get("_id")),
                "team_name": record.get("name", "—"),
                "league": member_of.get("name", "—") if isinstance(member_of, dict) else "—",
                "coach": ", ".join(coaches) if coaches else "—",
                "athletes": athletes_payload,
                "roster_count": len(athletes_payload),
                "stats": {
                    "avg_games_played": avg_gp,
                    "total_team_goals": total_goals,
                    "total_team_assists": total_assists,
                    "goalies": goalie_count,
                    "skaters": skater_count,
                    "cohort_season": record.get("season", f"{team_year}-{team_year + 1}"),
                    "top_scorer": top_scorer_name,
                    "top_assister": top_assister_name,
                    "top_goalie": top_goalie_name
                }
            })

        # Generate a cleaner query log string for debugging
        query_log = f"db.teams.find({str(query)})"
        return jsonify({"data": results, "total": len(results), "query": query_log}), 200
        
    except Exception as e:
        print(f"Error in get_teams_roster: {e}")
        return jsonify({"data": [], "total": 0, "query": "db.teams.find({})", "error": str(e)}), 500



# ---------------------------------------------------------------------------
# GET /api/filters/teams
# ---------------------------------------------------------------------------
@teams_bp.route("/api/filters/teams", methods=["GET"])
def get_team_filters():
    try:
        teams_collection = db.teams
        
        unique_teams = teams_collection.distinct("name")
        unique_leagues = (
            teams_collection.distinct("league") or 
            teams_collection.distinct("memberOf.name")
        )
        
        # 1. Pull the raw distinct values for your season field
        raw_seasons = teams_collection.distinct("year") 

        processed_seasons = []
        for s in raw_seasons:
            if s is None:
                continue
            
            # Convert to string and clean it up
            s_str = str(s).strip()
            if not s_str:
                continue
                
            try:
                # Try to convert to an integer if it's a standard numeric year (e.g., 2025)
                processed_seasons.append(int(float(s_str)))
            except ValueError:
                # If it's a custom string (e.g., "2024-2025"), keep it as a string
                processed_seasons.append(s_str)

        # 2. Sort the final cleaned array. We use a key to safely mix strings/ints if needed
        # It handles descending order (newest years at the top)
        sorted_seasons = sorted(
            list(set(processed_seasons)), 
            key=lambda x: str(x), 
            reverse=True
        )

        return jsonify({
            "teams": sorted([str(t).strip() for t in unique_teams if t]),
            "leagues": sorted([str(l).strip() for l in unique_leagues if l]),
            "seasons": sorted_seasons
        }), 200
        
    except Exception as e:
        # This will output the exact line and message causing issues to your terminal
        print(f"Error in get_team_filters: {e}")
        return jsonify({
            "error": "Failed to compile filter options",
            "teams": [],
            "leagues": [],
            "seasons": []
        }), 500
