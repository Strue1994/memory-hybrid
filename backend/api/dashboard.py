"""Memory Hybrid Web UI Dashboard — FastAPI router.

Serves a single-page dashboard with health status, sessions, rules,
stats, and a quick recall search box.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from .config import settings
from .review_flow import (
    candidate_review_dir,
    ensure_memory_root,
    list_rules,
    preferred_memory_root,
    review_log_path,
    rules_file_path,
)
from .scoring import score_temporal, recency_bonus

router = APIRouter(tags=["dashboard"])

# ── Helpers ──────────────────────────────────────────────────────


def _memory_root() -> Path:
    return preferred_memory_root() or ensure_memory_root()


def _count_files(path: Path, pattern: str = "*") -> int:
    if not path.exists():
        return 0
    return len(list(path.rglob(pattern)))


def _read_file_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


# ── Dashboard Data API ───────────────────────────────────────────


@router.get("/api/dashboard/data")
async def dashboard_data():
    """Return all dashboard data as JSON."""
    root = _memory_root()
    data: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "memory_root": str(root) if root else None,
    }

    # Health
    data["health"] = {
        "memory_root_exists": root.exists() if root else False,
    }

    # Stats
    if root and root.exists():
        data["stats"] = {
            "sessions": _count_files(root / "sessions", "*.md"),
            "candidates": _count_files(root / "hardening" / "candidates", "*.yaml"),
            "timeline_entries": _count_files(root / "timeline", "*.md"),
            "decisions": _count_files(root / "decisions", "*.md"),
            "facts": _count_files(root / "facts", "*.md"),
            "rules": 0,
        }

        # Rules count from rules.yaml
        rules_path = rules_file_path()
        if rules_path.exists():
            import yaml
            try:
                rules_data = yaml.safe_load(_read_file_text(rules_path))
                if isinstance(rules_data, dict):
                    data["stats"]["rules"] = len(rules_data.get("rules", []))
            except Exception:
                pass
    else:
        data["stats"] = {}

    # Recent sessions
    if root:
        sessions_dir = root / "sessions"
        if sessions_dir.exists():
            files = sorted(sessions_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
            data["sessions"] = [
                {
                    "filename": f.name,
                    "file_size": f.stat().st_size,
                    "modified_at": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat(),
                    "preview": _read_file_text(f)[:200],
                }
                for f in files
            ]
        else:
            data["sessions"] = []
    else:
        data["sessions"] = []

    # Rules list
    try:
        rules_list = list_rules(enabled=None, q=None)
        data["rules"] = rules_list.get("rules", [])[:50]
    except Exception:
        data["rules"] = []

    return data


# ── Dashboard Page ───────────────────────────────────────────────

_HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Memory Hybrid Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;background:#0d1117;color:#e6edf3;padding:24px}
h1{font-size:22px;font-weight:600}
.top-bar{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid #30363d}
.top-bar h1{display:flex;align-items:center;gap:12px}
.status-badge{display:inline-block;padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600}
.status-ok{background:#1a7f37;color:#fff}
.status-degraded{background:#d29922;color:#fff}
.status-error{background:#da3633;color:#fff}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin-bottom:24px}
.stat-card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px}
.stat-card .num{font-size:28px;font-weight:600;color:#58a6ff}
.stat-card .lbl{font-size:12px;color:#8b949e;margin-top:4px}
.panels{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px}
@media(max-width:768px){.panels{grid-template-columns:1fr}}
.panel{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px}
.panel h2{font-size:14px;font-weight:600;margin-bottom:12px;color:#58a6ff}
.session-item{padding:8px 0;border-bottom:1px solid #21262d;font-size:13px}
.session-item:last-child{border-bottom:0}
.session-item .name{color:#e6edf3;font-weight:500}
.session-item .meta{color:#8b949e;font-size:11px;margin-top:2px}
.session-item .preview{color:#8b949e;font-size:12px;margin-top:4px;max-height:40px;overflow:hidden}
.rule-table{width:100%;border-collapse:collapse;font-size:12px}
.rule-table th{text-align:left;padding:6px 8px;color:#8b949e;border-bottom:1px solid #21262d;font-weight:600}
.rule-table td{padding:6px 8px;border-bottom:1px solid #21262d;vertical-align:top}
.rule-table tr:hover td{background:#1c2128}
.enabled-tag{display:inline-block;padding:1px 6px;border-radius:8px;font-size:11px;font-weight:500}
.enabled-true{background:#1a7f37;color:#fff}
.enabled-false{background:#21262d;color:#8b949e}
.search-section{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:24px}
.search-section h2{font-size:14px;font-weight:600;margin-bottom:12px;color:#58a6ff}
.search-input{width:100%;padding:10px 14px;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;font-size:14px;outline:none}
.search-input:focus{border-color:#58a6ff}
.search-btn{margin-top:8px;padding:8px 20px;background:#238636;color:#fff;border:none;border-radius:6px;font-size:13px;font-weight:500;cursor:pointer}
.search-btn:hover{background:#2ea043}
.search-results{margin-top:12px}
.search-result-item{padding:8px;border:1px solid #21262d;border-radius:6px;margin-bottom:8px;font-size:13px}
.search-result-item .layer-tag{display:inline-block;padding:1px 6px;border-radius:4px;font-size:11px;background:#1f6feb;color:#fff;margin-right:8px}
.search-result-item .file{color:#8b949e;font-size:11px}
.search-result-item .snippet{color:#e6edf3;margin-top:4px;font-family:ui-monospace,monospace;font-size:12px;white-space:pre-wrap}
.loading{color:#8b949e;font-size:13px;padding:12px 0}
.error-msg{color:#da3633;font-size:13px}
.footer{text-align:center;color:#8b949e;font-size:11px;padding:16px 0 8px}
</style>
</head>
<body>
<div class="top-bar">
  <h1>Memory Hybrid Dashboard</h1>
  <span id="statusBadge" class="status-badge status-ok">OK</span>
</div>

<div class="stats-grid" id="statsGrid">
  <div class="stat-card"><div class="num">-</div><div class="lbl">Sessions</div></div>
  <div class="stat-card"><div class="num">-</div><div class="lbl">Rules</div></div>
  <div class="stat-card"><div class="num">-</div><div class="lbl">Candidates</div></div>
  <div class="stat-card"><div class="num">-</div><div class="lbl">Timeline Entries</div></div>
  <div class="stat-card"><div class="num">-</div><div class="lbl">Decisions</div></div>
  <div class="stat-card"><div class="num">-</div><div class="lbl">Facts</div></div>
</div>

<div class="panels">
  <div class="panel" id="sessionsPanel">
    <h2>Recent Sessions</h2>
    <div class="loading">Loading sessions...</div>
  </div>
  <div class="panel" id="rulesPanel">
    <h2>Hardening Rules</h2>
    <div class="loading">Loading rules...</div>
  </div>
</div>

<div class="search-section">
  <h2>Quick Recall Search</h2>
  <input id="searchInput" class="search-input" type="text" placeholder="Search memory content..." onkeydown="if(event.key==='Enter')doSearch()">
  <button class="search-btn" onclick="doSearch()">Search</button>
  <div id="searchResults" class="search-results"></div>
</div>

<div class="footer">
  Memory Hybrid &mdash; <span id="footerTime">-</span>
</div>

<script>
async function fetchJSON(url){
  const r=await fetch(url);
  if(!r.ok)throw new Error(r.statusText);
  return r.json();
}

function escapeHTML(s){
  if(!s)return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

async function loadDashboard(){
  try{
    const data=await fetchJSON('/api/dashboard/data');
    document.getElementById('footerTime').textContent=data.timestamp||new Date().toISOString();

    // Status badge
    const badge=document.getElementById('statusBadge');
    if(data.health&&data.health.memory_root_exists){
      badge.textContent='OK';
      badge.className='status-badge status-ok';
    }else{
      badge.textContent='Degraded';
      badge.className='status-badge status-degraded';
    }

    // Stats cards
    const st=data.stats||{};
    const cards=document.querySelectorAll('.stat-card .num');
    if(cards.length>=6){
      cards[0].textContent=st.sessions??'-';
      cards[1].textContent=st.rules??'-';
      cards[2].textContent=st.candidates??'-';
      cards[3].textContent=st.timeline_entries??'-';
      cards[4].textContent=st.decisions??'-';
      cards[5].textContent=st.facts??'-';
    }

    // Sessions panel
    const sessionsPanel=document.getElementById('sessionsPanel');
    if(data.sessions&&data.sessions.length>0){
      sessionsPanel.innerHTML='<h2>Recent Sessions</h2>'+
        data.sessions.map(s=>`
          <div class="session-item">
            <div class="name">${escapeHTML(s.filename)}</div>
            <div class="meta">${s.file_size}B &middot; ${s.modified_at||''}</div>
            <div class="preview">${escapeHTML(s.preview)}</div>
          </div>
        `).join('');
    }else{
      sessionsPanel.innerHTML='<h2>Recent Sessions</h2><div class="loading">No sessions found.</div>';
    }

    // Rules panel
    const rulesPanel=document.getElementById('rulesPanel');
    if(data.rules&&data.rules.length>0){
      rulesPanel.innerHTML='<h2>Hardening Rules</h2>'+
        `<table class="rule-table">
          <tr><th>ID</th><th>Trigger</th><th>Level</th><th>Enabled</th></tr>
          ${data.rules.map(r=>`
            <tr>
              <td>${escapeHTML(r.id||r.rule_id||'-')}</td>
              <td>${escapeHTML(r.trigger||r.pattern||'-')}</td>
              <td>${escapeHTML(r.level||'-')}</td>
              <td><span class="enabled-tag enabled-${r.enabled===false?'false':'true'}">${r.enabled===false?'Disabled':'Enabled'}</span></td>
            </tr>
          `).join('')}
        </table>`;
    }else{
      rulesPanel.innerHTML='<h2>Hardening Rules</h2><div class="loading">No rules found.</div>';
    }
  }catch(e){
    document.getElementById('statsGrid').innerHTML='<div class="error-msg">Failed to load dashboard: '+escapeHTML(e.message)+'</div>';
  }
}

async function doSearch(){
  const q=document.getElementById('searchInput').value.trim();
  const resultsDiv=document.getElementById('searchResults');
  if(!q){
    resultsDiv.innerHTML='';
    return;
  }
  resultsDiv.innerHTML='<div class="loading">Searching...</div>';
  try{
    const data=await fetchJSON('/api/dashboard/data?search='+encodeURIComponent(q));
    // Client-side search through sessions
    const results=[];
    if(data.sessions){
      for(const s of data.sessions){
        if((s.filename&&s.filename.toLowerCase().includes(q.toLowerCase()))||
           (s.preview&&s.preview.toLowerCase().includes(q.toLowerCase()))){
          results.push({layer:'L1',file:s.filename,snippet:s.preview});
        }
      }
    }
    if(results.length===0){
      resultsDiv.innerHTML='<div class="loading">No results found.</div>';
    }else{
      resultsDiv.innerHTML=results.map(r=>`
        <div class="search-result-item">
          <span class="layer-tag">${r.layer}</span>
          <span class="file">${escapeHTML(r.file)}</span>
          <div class="snippet">${escapeHTML(r.snippet)}</div>
        </div>
      `).join('');
    }
  }catch(e){
    resultsDiv.innerHTML='<div class="error-msg">Search failed: '+escapeHTML(e.message)+'</div>';
  }
}

loadDashboard();
setInterval(loadDashboard,30000);
</script>
</body>
</html>"""


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    """Render the web UI dashboard."""
    return HTMLResponse(_HTML_PAGE)
