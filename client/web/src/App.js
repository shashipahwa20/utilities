import React, { useState, useEffect, useCallback } from 'react';
import MetricCards from './components/MetricCards';
import Filters from './components/Filters';
import PlayerTable from './components/PlayerTable';
import PlayerCompare from './components/PlayerCompare';
import QueryDisplay from './components/QueryDisplay';
import TeamView from './components/TeamView';
import LeaguesView from './components/LeaguesView';
import VacanciesPage from './components/VacanciesPage';
import RecruitingSchools from './components/RecruitingSchools';
import PlayerProfileDrawer from './components/PlayerProfileDrawer';
import useDebounce from './hooks/useDebounce';
import { fetchFilters, fetchPlayers, fetchTeamFilters, fetchTeams } from './api';
import styles from './App.module.css';

// ── Default filter states ────────────────────────────────────────────────────
const DEFAULT_PLAYER_FILTERS = {
  birthYearFrom: '', birthYearTo: '',
  season: '', league: '', position: '', search: '',
  page: 0, sortBy: 'player_name', sortDir: 'asc',
};

const DEFAULT_TEAM_FILTERS = {
  season: '', search: '', athlete: '', coach: '', league: '',
  page: 0, sortBy: 'team_name', sortDir: 'asc',
};

// ── Nav items ────────────────────────────────────────────────────────────────
const NAV = [
  { id: 'dashboard', label: 'Dashboard', icon: '◈' },
  { id: 'players',   label: 'Players',   icon: '◇' },
  { id: 'teams',     label: 'Teams',     icon: '◉' },
  { id: 'leagues',   label: 'Leagues',   icon: '◎' },
  { id: 'recruiting', label: 'Recruiting', icon: '⊙' },
  { id: 'vacancies', label: 'Vacancies', icon: '◍' },
];

export default function App() {
  const [activeView, setActiveView] = useState('dashboard');

  // ── Filter options (dropdown data) ──
  const [filterOptions,     setFilterOptions]     = useState({});
  const [teamFilterOptions, setTeamFilterOptions] = useState({});

  // ── Active filter values ──
  const [playerFilters, setPlayerFilters] = useState(DEFAULT_PLAYER_FILTERS);
  const [teamFilters,   setTeamFilters]   = useState(DEFAULT_TEAM_FILTERS);

  // ── Data ──
  const [players, setPlayers] = useState({ data: [], total: 0, query: '' });
  const [teams,   setTeams]   = useState({ data: [], total: 0, query: '' });
  const [metrics, setMetrics] = useState({});

  // ── UI state ──
  const [loading,       setLoading]       = useState(false);
  const [error,         setError]         = useState(null);
  const [profilePlayer, setProfilePlayer] = useState(null); // name string or null

  const debouncedPlayerSearch = useDebounce(playerFilters.search, 400);
  const debouncedTeamSearch   = useDebounce(teamFilters.search,   400);

  // ── Boot: load dropdown options once ──
  useEffect(() => {
    fetchFilters().then(setFilterOptions).catch(console.error);
    fetchTeamFilters().then(setTeamFilterOptions).catch(console.error);
  }, []);

  // ── Data loader ──────────────────────────────────────────────────────────
  // IMPORTANT: playerFilters / teamFilters objects are NOT listed as deps.
  // Listing the whole object causes loadData to rebuild on every keystroke,
  // firing a fetch with the stale debouncedSearch value before the debounce
  // settles — resulting in a flash of full data followed by the real results.
  // Instead, each primitive value is listed individually, and search always
  // goes through its debounced counterpart.
  const loadData = useCallback(async () => {
    if (activeView === 'teams') {
      setLoading(true);
      setError(null);
      try {
        const t = await fetchTeams({
          season:   teamFilters.season,
          search:   debouncedTeamSearch,
          athlete:  teamFilters.athlete,
          coach:    teamFilters.coach,
          league:   teamFilters.league,
          page:     teamFilters.page,
          pageSize: 50,
          sortBy:   teamFilters.sortBy,
          sortDir:  teamFilters.sortDir,
        });
        setTeams(t);
      } catch (err) { setError(err.message); }
      finally       { setLoading(false); }
      return;
    }

    if (activeView === 'dashboard' || activeView === 'players') {
      setLoading(true);
      setError(null);
      try {
        const res = await fetchPlayers({
          birthYearFrom: playerFilters.birthYearFrom,
          birthYearTo:   playerFilters.birthYearTo,
          // backward-compat: pass single birthYear if only one end is set
          birthYear:     (!playerFilters.birthYearTo && playerFilters.birthYearFrom)
                           ? playerFilters.birthYearFrom : undefined,
          season:        playerFilters.season,
          league:        playerFilters.league,
          position:      playerFilters.position,
          search:        debouncedPlayerSearch,
          page:          playerFilters.page,
          pageSize:      50,
          sortBy:        playerFilters.sortBy,
          sortDir:       playerFilters.sortDir,
        });
        if (res) {
          
          setPlayers(res);
          if (res.metrics) setMetrics(res.metrics);
        }
      } catch (err) { setError(err.message); }
      finally       { setLoading(false); }
    }
  }, [
    activeView,
    // Player filter primitives — search intentionally excluded (uses debounced below)
    playerFilters.birthYearFrom,
    playerFilters.birthYearTo,
    playerFilters.season,
    playerFilters.league,
    playerFilters.position,
    playerFilters.page,
    playerFilters.sortBy,
    playerFilters.sortDir,
    debouncedPlayerSearch,   // only fires after the 400 ms debounce settles
    // Team filter primitives
    teamFilters.athlete,
    teamFilters.coach,
    teamFilters.league,
    teamFilters.page,
    teamFilters.sortBy,
    teamFilters.sortDir,
    debouncedTeamSearch,
  ]);

  useEffect(() => { loadData(); }, [loadData]);

  // ── Sort handlers ────────────────────────────────────────────────────────
  const handlePlayerSort = (key) => setPlayerFilters(f => ({
    ...f,
    sortBy:  key,
    sortDir: f.sortBy === key && f.sortDir === 'asc' ? 'desc' : 'asc',
    page:    0,
  }));

  const handleTeamSort = (key) => setTeamFilters(f => ({
    ...f,
    sortBy:  key,
    sortDir: f.sortBy === key && f.sortDir === 'asc' ? 'desc' : 'asc',
    page:    0,
  }));

  // ── View renderer ────────────────────────────────────────────────────────
  const renderView = () => {
    switch (activeView) {

      case 'dashboard':
        return (
          <>
            <MetricCards metrics={metrics} loading={loading} onPlayerClick={setProfilePlayer} />
            <Filters
              filters={filterOptions}
              values={playerFilters}
              onChange={setPlayerFilters}
            />
            <QueryDisplay query={players.query || ''} />
            <PlayerTable
              data={players.data || []}
              total={players.total || 0}
              page={playerFilters.page}
              pageSize={50}
              loading={loading}
              sortBy={playerFilters.sortBy}
              sortDir={playerFilters.sortDir}
              onSort={handlePlayerSort}
              onPage={(p) => setPlayerFilters(f => ({ ...f, page: p }))}
              currentPositionFilter={playerFilters.position}
              onPlayerClick={setProfilePlayer}
              birthYearFrom={playerFilters.birthYearFrom}
              birthYearTo={playerFilters.birthYearTo}
              season={playerFilters.season}
              league={playerFilters.league}
              position={playerFilters.position}
              search={playerFilters.search}
            />
          </>
        );

      case 'players':
        return <PlayerCompare onPlayerClick={setProfilePlayer} />;

      case 'teams':
        return (
          <TeamView
            filterOptions={teamFilterOptions}
            filters={teamFilters}
            setFilters={setTeamFilters}
            teams={teams}
            loading={loading}
            handleSort={handleTeamSort}
          />
        );

      case 'leagues':
        return <LeaguesView />;
      
      case 'recruiting':
        return <RecruitingSchools />;

      case 'vacancies': 
        return <VacanciesPage />

      default:
        return null;
    }
  };

  const activeNav = NAV.find(n => n.id === activeView);

  return (
    <div className={styles.layout}>

      {/* ── Sidebar ──────────────────────────────────────────── */}
      <aside className={styles.sidebar}>
        <div className={styles.brand}>
          <div className={styles.brandTop}>
            <div className={styles.brandIcon}>TopPlay</div>
            <div className={styles.brandName}>Scout DB</div>
          </div>
          <div className={styles.brandSub}>Player Analytics</div>
        </div>

        <nav className={styles.nav}>
          <div className={styles.navLabel}>Views</div>
          {NAV.map(({ id, label, icon }) => (
            <button
              key={id}
              type="button"
              className={`${styles.navItem} ${activeView === id ? styles.navActive : ''}`}
              onClick={() => setActiveView(id)}
            >
              <span className={styles.navIcon}>{icon}</span> {label}
            </button>
          ))}
        </nav>

        <div className={styles.sideStats}>
          <div className={styles.sideStatsTitle}>Summary</div>
          {[
            ['Total Players', metrics.totalPlayers?.toLocaleString() ?? '—'],
            ['Forwards',      metrics.forwards?.toLocaleString()     ?? '—'],
            ['Defensemen',    metrics.defensemen?.toLocaleString()   ?? '—'],
            ['Goalies',       metrics.goalies?.toLocaleString()      ?? '—'],
            ['Leagues',       metrics.leagueCount                    ?? '—'],
          ].map(([label, val]) => (
            <div key={label} className={styles.sideStatRow}>
              <span className={styles.sideStatLabel}>{label}</span>
              <span className={styles.sideStatVal}>{val}</span>
            </div>
          ))}
        </div>
      </aside>

      {/* ── Main ─────────────────────────────────────────────── */}
      <main className={styles.main}>
        <div className={styles.topbar}>
          <div className={styles.titleGroup}>
            <h1 className={styles.pageTitle}>{activeNav?.label}</h1>
            <span className={styles.pageSubtitle}>
              {activeView === 'dashboard'
                ? 'Query by birth year, league, season & position'
                : activeView === 'players'
                ? 'Search and compare two players head-to-head'
                : `Overview breakdown for active database ${activeView}`}
            </span>
          </div>
          {error && <div className={styles.error}>⚠ {error}</div>}
        </div>

        <div className={styles.content}>
          {renderView()}
        </div>
      </main>

      {/* ── Player profile drawer (global overlay) ── */}
      {profilePlayer && (
        <PlayerProfileDrawer
          playerName={profilePlayer}
          onClose={() => setProfilePlayer(null)}
        />
      )}
    </div>
  );
}
