import React from 'react';
import styles from './Filters.module.css';

export default function TeamFilters({ filterOptions, values, onChange }) {
  const handleChange = (field, val) => {
    onChange(prev => ({ ...prev, [field]: val, page: 0 }));
  };

  const uniqueTeams = [...new Set(filterOptions.teams || [])].sort();
  // Sort seasons in descending order so the newest years appear first
  const sortedSeasons = [...new Set(filterOptions.seasons || [])].sort((a, b) => b - a);

  return (
    <div className={styles.bar}>
      {/* --- Added Season Dropdown --- */}
      <div className={styles.group}>
        <label className={styles.label}>Season</label>
        <select 
          className={styles.select} 
          value={values.season || ''} 
          onChange={(e) => handleChange('season', e.target.value)}
        >
          <option value="">All Seasons</option>
          {sortedSeasons.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </div>

      <div className={styles.group}>
        <label className={styles.label}>Team Name</label>
        <select 
          className={styles.select} 
          value={values.search || ''} 
          onChange={(e) => handleChange('search', e.target.value)}
        >
          <option value="">All Teams</option>
          {uniqueTeams.map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
      </div>

      <div className={styles.group}>
        <label className={styles.label}>Roster Athlete</label>
        <input 
          type="text" 
          className={styles.input} 
          placeholder="Search by athlete name…" 
          value={values.athlete || ''} 
          onChange={(e) => handleChange('athlete', e.target.value)} 
        />
      </div>

      <div className={styles.group}>
        <label className={styles.label}>Head Coach</label>
        <input 
          type="text" 
          className={styles.input} 
          placeholder="Filter by coach…" 
          value={values.coach || ''} 
          onChange={(e) => handleChange('coach', e.target.value)} 
        />
      </div>

      <div className={styles.group}>
        <label className={styles.label}>League</label>
        <select 
          className={styles.select} 
          value={values.league || ''} 
          onChange={(e) => handleChange('league', e.target.value)} 
        >
          <option value="">All Leagues</option>
          {(filterOptions.leagues || []).map((l) => (
            <option key={l} value={l}>{l}</option>
          ))}
        </select>
      </div>

      <button 
        className={styles.reset} 
        onClick={() => onChange(() => ({ 
          season: '', // Reset season key
          search: '', 
          athlete: '', 
          coach: '', 
          league: '', 
          page: 0, 
          sortBy: 'team_name', 
          sortDir: 'asc' 
        }))} 
      >
        Clear all
      </button>
    </div>
  );
}
