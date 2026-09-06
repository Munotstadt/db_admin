/**
 * DB Admin – Improvements Button
 * Einbindung in jedes Tool/HTML:
 *   <script src="https://munotstadt.github.io/db_admin/assets/improvements-button.js"
 *           data-app="mein-tool-name"></script>
 *
 * Erzeugt einen fixierten Button unten rechts. Klick öffnet ein Popup,
 * das einen neuen Task im Cloudflare-D1-Task-Log anlegt (via Worker-API).
 */
(function () {
  const API_BASE = "https://db-admin-tasks.ph-gnaedinger.workers.dev";
  const scriptTag = document.currentScript;
  const appName = (scriptTag && scriptTag.dataset.app) || document.title || "unknown-app";
  const position = (scriptTag && scriptTag.dataset.position) || "right"; // "left" oder "right"

  const style = document.createElement('style');
  style.textContent = `
    #imp-btn{position:fixed;bottom:20px;z-index:99998;
      background:#E30613;color:#fff;border:none;border-radius:30px;
      padding:.7rem 1.1rem;font-family:Inter,sans-serif;font-size:.85rem;
      box-shadow:0 2px 10px rgba(0,0,0,.2);cursor:pointer;display:flex;
      align-items:center;gap:.4rem;}
    #imp-btn.right{right:20px;}
    #imp-btn.left{left:20px;}
    #imp-btn:hover{background:#c00510;}
    #imp-overlay{position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:99999;
      display:flex;align-items:center;justify-content:center;}
    #imp-modal{background:#fff;border-radius:10px;padding:1.4rem;width:min(420px,90vw);
      font-family:Inter,sans-serif;}
    #imp-modal h3{margin:0 0 .8rem;font-family:'Space Grotesk',sans-serif;}
    #imp-modal label{display:block;font-size:.8rem;color:#555;margin:.6rem 0 .2rem;}
    #imp-modal input,#imp-modal textarea,#imp-modal select{width:100%;padding:.5rem;
      border:1px solid #ddd;border-radius:6px;font-family:inherit;box-sizing:border-box;}
    #imp-modal textarea{min-height:80px;resize:vertical;}
    #imp-modal .imp-actions{display:flex;justify-content:flex-end;gap:.5rem;margin-top:1rem;}
    #imp-modal button{padding:.5rem 1rem;border-radius:6px;border:1px solid #ddd;
      background:#fff;cursor:pointer;font-family:inherit;}
    #imp-modal .imp-save{background:#E30613;color:#fff;border-color:#E30613;}
    #imp-status{font-size:.8rem;margin-top:.5rem;color:#555;}
  `;
  document.head.appendChild(style);

  const btn = document.createElement('button');
  btn.id = 'imp-btn';
  btn.className = position === 'left' ? 'left' : 'right';
  btn.innerHTML = '✎ Improvements';
  document.body.appendChild(btn);

  function openModal() {
    const overlay = document.createElement('div');
    overlay.id = 'imp-overlay';
    overlay.innerHTML = `
      <div id="imp-modal">
        <h3>Neuer Improvement-Task</h3>
        <label>Titel</label>
        <input id="imp-title" placeholder="Kurze Zusammenfassung">
        <label>Notiz</label>
        <textarea id="imp-note" placeholder="Was soll verbessert werden?"></textarea>
        <label>Priorität</label>
        <select id="imp-prio">
          <option value="low">Niedrig</option>
          <option value="normal" selected>Normal</option>
          <option value="high">Hoch</option>
        </select>
        <div id="imp-status"></div>
        <div class="imp-actions">
          <button id="imp-cancel">Abbrechen</button>
          <button id="imp-save" class="imp-save">Speichern</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    document.getElementById('imp-title').focus();

    overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
    document.getElementById('imp-cancel').onclick = () => overlay.remove();
    document.getElementById('imp-save').onclick = async () => {
      const title = document.getElementById('imp-title').value.trim();
      const note = document.getElementById('imp-note').value.trim();
      const priority = document.getElementById('imp-prio').value;
      const statusEl = document.getElementById('imp-status');
      if (!title) { statusEl.textContent = 'Bitte einen Titel eingeben.'; return; }
      statusEl.textContent = 'Speichere…';
      try {
        const res = await fetch(`${API_BASE}/tasks`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            sourceApp: appName,
            sourceUrl: window.location.href,
            title, note, priority
          })
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        statusEl.textContent = 'Gespeichert ✓';
        setTimeout(() => overlay.remove(), 700);
      } catch (e) {
        statusEl.innerHTML = 'Nicht eingeloggt oder Verbindung fehlgeschlagen. ' +
          '<a href="' + API_BASE + '/tasks" target="_blank" rel="noopener">Hier einloggen</a> ' +
          'und danach nochmal auf Speichern klicken.';
      }
    };
  }

  btn.onclick = openModal;
})();
