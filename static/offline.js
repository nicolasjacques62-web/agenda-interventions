/**
 * HPS3D — Gestionnaire hors-ligne
 *
 * Fonctionnement :
 *  1. Quand offline : les photos ET les formulaires (bon d'intervention,
 *     signatures) sont stockés localement (IndexedDB) avec un badge
 *     "En attente" visible dans la barre du haut.
 *  2. Quand online  : synchronisation automatique → envoi au serveur,
 *     les badges "En attente" disparaissent.
 *
 * Exposed globals : pendingPhotosAdd, appendPendingPhotoCard, showToast,
 * updateBadge, envoyerPhotoBon, rendreFormulaireHorsLigneCompatible
 */

(function () {
  'use strict';

  const DB_NAME    = 'hps3d-offline';
  const DB_VERSION = 2;

  // ── IndexedDB ────────────────────────────────────────────────

  function openDB() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = e => {
        const db = e.target.result;
        if (!db.objectStoreNames.contains('pendingPhotos')) {
          const s = db.createObjectStore('pendingPhotos', { keyPath: 'id' });
          s.createIndex('bonId', 'bonId', { unique: false });
        }
        if (!db.objectStoreNames.contains('pendingForms')) {
          db.createObjectStore('pendingForms', { keyPath: 'key' });
        }
      };
      req.onsuccess = e => resolve(e.target.result);
      req.onerror   = () => reject(req.error);
    });
  }

  // ── Formulaires en attente (bon d'intervention, signatures, ...) ──
  // Une seule entrée par "key" (ex: bon-modifier-42) : si le technicien
  // enregistre plusieurs fois hors-ligne, seule la dernière version est
  // conservée et envoyée à la reconnexion.

  async function pendingFormPut(key, url, pairs, label) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction('pendingForms', 'readwrite');
      tx.objectStore('pendingForms').put({
        key, url, pairs, label, timestamp: new Date().toISOString(),
      });
      tx.oncomplete = () => resolve();
      tx.onerror    = () => reject(tx.error);
    });
  }

  async function pendingFormsGetAll() {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx  = db.transaction('pendingForms', 'readonly');
      const req = tx.objectStore('pendingForms').getAll();
      req.onsuccess = e => resolve(e.target.result || []);
      req.onerror   = () => reject(req.error);
    });
  }

  async function pendingFormDelete(key) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx  = db.transaction('pendingForms', 'readwrite');
      const req = tx.objectStore('pendingForms').delete(key);
      req.onsuccess = resolve;
      req.onerror   = () => reject(req.error);
    });
  }

  window.pendingPhotosAdd = async function (bonId, filename, dataUrl, mimetype) {
    const db = await openDB();
    const id = Date.now() + '-' + Math.random().toString(36).slice(2, 8);
    return new Promise((resolve, reject) => {
      const tx  = db.transaction('pendingPhotos', 'readwrite');
      const req = tx.objectStore('pendingPhotos').add({
        id, bonId, filename, dataUrl, mimetype,
        timestamp: new Date().toISOString(),
      });
      req.onsuccess = () => resolve(id);
      req.onerror   = () => reject(req.error);
    });
  };

  async function pendingPhotosGetAll(bonId) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx    = db.transaction('pendingPhotos', 'readonly');
      const store = tx.objectStore('pendingPhotos');
      const req   = (bonId != null)
        ? store.index('bonId').getAll(IDBKeyRange.only(bonId))
        : store.getAll();
      req.onsuccess = e => resolve(e.target.result || []);
      req.onerror   = () => reject(req.error);
    });
  }

  async function pendingPhotosDelete(id) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx  = db.transaction('pendingPhotos', 'readwrite');
      const req = tx.objectStore('pendingPhotos').delete(id);
      req.onsuccess = resolve;
      req.onerror   = () => reject(req.error);
    });
  }

  // ── CSRF token ───────────────────────────────────────────────

  async function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) return meta.getAttribute('content');
    return getCsrfTokenFrais();
  }

  // Toujours interroger le serveur pour un jeton CSRF à jour — utilisé lors
  // d'une synchronisation hors-ligne, où le jeton de la page (chargée avant
  // la coupure réseau, parfois plus d'1h auparavant) peut avoir expiré.
  async function getCsrfTokenFrais() {
    try {
      const resp = await fetch('/api/csrf-token', { credentials: 'include' });
      const data = await resp.json();
      return data.csrf_token;
    } catch (_) {
      return '';
    }
  }

  // ── Envoi d'une photo de bon (en ligne ou hors-ligne) ─────────
  // Utilisée à la fois par la fiche du bon et par son formulaire de
  // modification : lit le fichier, l'envoie tout de suite si le réseau
  // est là, sinon le met en file d'attente (IndexedDB) pour un envoi
  // automatique à la reconnexion. Dans les deux cas, la vignette
  // apparaît immédiatement dans la grille #photos-grid de la page.
  window.envoyerPhotoBon = async function (bonId, file) {
    if (!file) return;
    const dataUrl = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload  = e => resolve(e.target.result);
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });

    if (navigator.onLine) {
      try {
        const csrf = await getCsrfToken();
        const resp = await fetch('/api/bons/' + bonId + '/photos', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
          credentials: 'include',
          body: JSON.stringify({ nom: file.name, data: dataUrl, mimetype: file.type }),
        });
        if (resp.ok) {
          const result = await resp.json();
          _appendRealPhotoCard(result.photo_id, dataUrl, result.created_at);
          showToast('📸 Photo enregistrée.', 'success');
          return;
        }
      } catch (_) {
        // Erreur réseau malgré navigator.onLine → on bascule sur le stockage local ci-dessous
      }
    }

    const pendingId = await pendingPhotosAdd(bonId, file.name, dataUrl, file.type);
    appendPendingPhotoCard(pendingId, dataUrl, file.name);
    updateBadge();
    showToast('📶 Hors ligne — photo sauvegardée. Elle sera envoyée automatiquement à la reconnexion.', 'info');
  };

  // ── Préchargement du planning (consultation hors-ligne) ───────
  // Tant qu'on est en ligne, on rafraîchit silencieusement en arrière-plan
  // le tableau de bord, la liste des interventions et l'agenda (semaine en
  // cours) — le Service Worker les met alors en cache automatiquement.
  // Ainsi le planning reste consultable même si on n'a pas ouvert ces pages
  // soi-même juste avant de perdre le réseau.

  function _rangeSemaine() {
    const debut = new Date();
    debut.setHours(0, 0, 0, 0);
    debut.setDate(debut.getDate() - 1); // marge d'un jour avant
    const fin = new Date(debut);
    fin.setDate(fin.getDate() + 9); // ~9 jours de visibilité
    return { start: debut.toISOString(), end: fin.toISOString() };
  }

  async function precacherPlanning() {
    if (!navigator.onLine) return;
    // Évite de relancer les 5 requêtes à chaque navigation — une fois par
    // minute suffit largement pour garder le planning à jour.
    try {
      const dernier = parseInt(sessionStorage.getItem('planningPrecacheAt') || '0', 10);
      if (Date.now() - dernier < 60000) return;
      sessionStorage.setItem('planningPrecacheAt', String(Date.now()));
    } catch (_) { /* sessionStorage indisponible → on précharge quand même */ }
    const { start, end } = _rangeSemaine();
    const urls = [
      '/dashboard',
      '/interventions',
      '/interventions?vue=dossiers',
      '/agenda',
      '/agenda/api/events?start=' + encodeURIComponent(start) + '&end=' + encodeURIComponent(end),
    ];
    await Promise.all(urls.map(u =>
      fetch(u, { credentials: 'include' }).catch(() => {})
    ));
  }
  window.precacherPlanning = precacherPlanning;

  // ── Formulaires hors-ligne génériques (bon d'intervention, signatures) ──
  // Rend un <form> existant utilisable sans réseau : à l'enregistrement,
  // on tente l'envoi normal ; en cas d'échec (ou si on est déjà hors-ligne),
  // le contenu du formulaire est mis en file d'attente localement et envoyé
  // automatiquement à la reconnexion, sans perdre ce qui a été saisi.
  // `opts.key` doit être stable et unique pour ce formulaire (ex: 'bon-42')
  // — un nouvel enregistrement hors-ligne du même formulaire remplace le
  // précédent au lieu de s'empiler. `opts.label` sert juste à l'affichage.
  window.rendreFormulaireHorsLigneCompatible = function (form, opts) {
    if (!form) return;
    const key   = opts && opts.key   || form.id || form.action;
    const label = opts && opts.label || 'Formulaire';

    form.addEventListener('submit', async function (e) {
      e.preventDefault();
      const url   = form.getAttribute('action') || window.location.pathname;
      const pairs = [...new FormData(form).entries()]
        .filter(([, v]) => !(v instanceof File)); // les photos suivent leur propre file d'attente

      if (navigator.onLine) {
        try {
          const fd = new FormData();
          pairs.forEach(([k, v]) => fd.append(k, v));
          const resp = await fetch(url, { method: 'POST', body: fd, credentials: 'include' });
          if (resp.ok) {
            window.location.href = resp.url || url;
            return;
          }
        } catch (_) {
          // Pas de réseau malgré navigator.onLine → on bascule ci-dessous
        }
      }

      await pendingFormPut(key, url, pairs, label);
      updateBadge();
      showToast('📶 Hors ligne — "' + label + '" enregistré localement. Il sera envoyé automatiquement à la reconnexion.', 'info');
    });
  };

  function _envoyerPaires(url, pairs) {
    const fd = new FormData();
    pairs.forEach(([k, v]) => fd.append(k, v));
    return fetch(url, { method: 'POST', body: fd, credentials: 'include' });
  }

  async function syncPendingForms() {
    const pending = await pendingFormsGetAll();
    if (!pending.length) return;
    let ok = 0;
    let doitRecharger = false;
    for (const item of pending) {
      try {
        let resp = await _envoyerPaires(item.url, item.pairs);
        // Un bon resté hors-ligne plus d'1h a un jeton CSRF expiré (le jeton
        // est valable 1h) : on en récupère un frais et on retente une fois,
        // sans quoi la synchronisation échouerait silencieusement pour de bon.
        if (!resp.ok && (resp.status === 400 || resp.status === 403)) {
          const frais = await getCsrfTokenFrais();
          if (frais) {
            const pairsFrais = item.pairs.map(([k, v]) => (k === 'csrf_token' ? [k, frais] : [k, v]));
            resp = await _envoyerPaires(item.url, pairsFrais);
          }
        }
        if (resp.ok) {
          await pendingFormDelete(item.key);
          ok++;
          if (resp.url && new URL(resp.url).pathname === window.location.pathname) doitRecharger = true;
        }
      } catch (err) {
        console.warn('[Offline] Form sync failed:', item.key, err);
      }
    }
    if (ok > 0) {
      showToast('✅ ' + ok + ' formulaire(s) synchronisé(s) avec le serveur.', 'success');
      if (doitRecharger) window.location.reload();
    }
    updateBadge();
  }
  window.syncPendingForms = syncPendingForms;

  // ── Synchronisation ──────────────────────────────────────────

  let _syncing = false;

  async function syncAll() {
    if (_syncing || !navigator.onLine) return;
    _syncing = true;
    updateBadge();

    try {
      await syncPendingForms();

      const pending = await pendingPhotosGetAll(null);
      if (!pending.length) { _syncing = false; updateBadge(); return; }

      let csrf = await getCsrfToken();
      let syncedCount = 0;

      const envoyerPhoto = (photo, jeton) => fetch('/api/bons/' + photo.bonId + '/photos', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken':  jeton,
        },
        credentials: 'include',
        body: JSON.stringify({
          nom:      photo.filename,
          data:     photo.dataUrl,
          mimetype: photo.mimetype,
        }),
      });

      for (const photo of pending) {
        try {
          let resp = await envoyerPhoto(photo, csrf);
          // Photo restée hors-ligne plus d'1h → jeton CSRF expiré : on en
          // récupère un frais (celui de la page peut être ancien) et on retente.
          if (!resp.ok && (resp.status === 400 || resp.status === 403)) {
            csrf = await getCsrfTokenFrais();
            resp = await envoyerPhoto(photo, csrf);
          }

          if (resp.ok) {
            const result = await resp.json();
            await pendingPhotosDelete(photo.id);
            syncedCount++;

            // Met à jour la page si on est sur le bon correspondant
            const match = window.location.pathname.match(/\/bons\/(\d+)$/);
            if (match && parseInt(match[1]) === parseInt(photo.bonId)) {
              const pendingCard = document.querySelector('[data-pending-id="' + photo.id + '"]');
              if (pendingCard) pendingCard.remove();
              _appendRealPhotoCard(result.photo_id, photo.dataUrl, result.created_at);
            }
          }
        } catch (err) {
          console.warn('[Offline] Photo sync failed:', photo.id, err);
        }
      }

      if (syncedCount > 0) {
        showToast('✅ ' + syncedCount + ' photo(s) synchronisée(s) avec le serveur.', 'success');
      }
    } catch (e) {
      console.error('[Offline] syncAll error:', e);
    }

    _syncing = false;
    updateBadge();
  }

  // ── Badge topbar ─────────────────────────────────────────────

  window.updateBadge = async function () {
    const badge = document.getElementById('offline-badge');
    if (!badge) return;

    if (!navigator.onLine) {
      badge.innerHTML = '<i class="bi bi-wifi-off me-1"></i>Hors ligne';
      badge.className = 'badge rounded-pill bg-danger d-inline-flex align-items-center gap-1 py-2 px-3';
      badge.style.display = 'inline-flex';
      badge.onclick = null;
      badge.style.cursor = 'default';
      return;
    }

    if (_syncing) {
      badge.innerHTML = '<span class="spinner-border spinner-border-sm me-1" style="width:.7rem;height:.7rem"></span>Synchronisation…';
      badge.className = 'badge rounded-pill bg-warning text-dark d-inline-flex align-items-center gap-1 py-2 px-3';
      badge.style.display = 'inline-flex';
      return;
    }

    let pendingPhotos = [];
    let pendingForms  = [];
    try { pendingPhotos = await pendingPhotosGetAll(null); } catch (_) {}
    try { pendingForms  = await pendingFormsGetAll(); } catch (_) {}
    const total = pendingPhotos.length + pendingForms.length;

    if (total > 0) {
      const morceaux = [];
      if (pendingPhotos.length) morceaux.push(pendingPhotos.length + ' photo(s)');
      if (pendingForms.length)  morceaux.push(pendingForms.length + ' formulaire(s)');
      badge.innerHTML = '<i class="bi bi-cloud-upload me-1"></i>' + morceaux.join(', ') + ' en attente';
      badge.className = 'badge rounded-pill bg-warning text-dark d-inline-flex align-items-center gap-1 py-2 px-3';
      badge.style.display = 'inline-flex';
      badge.title      = 'Cliquez pour synchroniser maintenant';
      badge.style.cursor = 'pointer';
      badge.onclick    = syncAll;
    } else {
      badge.style.display = 'none';
    }
  };

  // ── Toast notification ───────────────────────────────────────

  window.showToast = function (msg, type) {
    const el = document.createElement('div');
    el.className = 'alert alert-' + (type === 'success' ? 'success' : 'info') +
      ' alert-dismissible position-fixed bottom-0 end-0 m-3 shadow-lg';
    el.style.cssText = 'z-index:9999;min-width:280px;max-width:420px;border-radius:12px;';
    el.innerHTML = msg + '<button type="button" class="btn-close" data-bs-dismiss="alert"></button>';
    document.body.appendChild(el);
    setTimeout(() => { if (el.parentNode) el.remove(); }, 6000);
  };

  // ── Cartes photos ────────────────────────────────────────────

  function _getOrCreateGrid() {
    let grid = document.getElementById('photos-grid');
    if (!grid) return null;
    // Retire le message "aucune photo"
    const empty = grid.querySelector('.empty-photos');
    if (empty) empty.remove();
    return grid;
  }

  /** Affiche une photo déjà synchronisée (vraie URL serveur). */
  function _appendRealPhotoCard(photoId, dataUrl, createdAt) {
    const grid = _getOrCreateGrid();
    if (!grid) return;
    if (document.getElementById('photo-card-' + photoId)) return; // déjà présente

    const timeStr = createdAt
      ? new Date(createdAt).toLocaleString('fr-FR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
      : '';

    const col = document.createElement('div');
    col.className = 'col-6 col-md-4 col-lg-3';
    col.id = 'photo-card-' + photoId;
    col.innerHTML =
      '<div class="position-relative">' +
        '<a href="/bons/photos/' + photoId + '" target="_blank">' +
          '<img src="' + dataUrl + '" alt="photo" class="img-thumbnail w-100" style="height:120px;object-fit:cover;">' +
        '</a>' +
        '<div class="position-absolute bottom-0 start-0 end-0 p-1" ' +
             'style="background:rgba(0,0,0,.55);font-size:.7rem;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' +
          timeStr +
        '</div>' +
      '</div>';
    grid.appendChild(col);
  }

  /** Affiche une photo en attente (stockée localement). */
  window.appendPendingPhotoCard = function (pendingId, dataUrl, filename) {
    const grid = _getOrCreateGrid();
    if (!grid) return;

    const col = document.createElement('div');
    col.className = 'col-6 col-md-4 col-lg-3';
    col.setAttribute('data-pending-id', pendingId);
    col.innerHTML =
      '<div class="position-relative">' +
        '<img src="' + dataUrl + '" alt="' + filename + '" ' +
             'class="img-thumbnail w-100" style="height:120px;object-fit:cover;opacity:.7;">' +
        '<div class="position-absolute top-0 start-0 end-0 text-center pt-1">' +
          '<span class="badge bg-warning text-dark" style="font-size:.65rem;">' +
            '<i class="bi bi-clock me-1"></i>En attente de connexion' +
          '</span>' +
        '</div>' +
        '<div class="position-absolute bottom-0 start-0 end-0 p-1" ' +
             'style="background:rgba(0,0,0,.55);font-size:.7rem;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' +
          filename +
        '</div>' +
      '</div>';
    grid.appendChild(col);
  };

  // ── Initialisation ───────────────────────────────────────────

  window.addEventListener('online',  () => { updateBadge(); syncAll(); precacherPlanning(); });
  window.addEventListener('offline', () => updateBadge());

  document.addEventListener('DOMContentLoaded', async () => {
    // Afficher le badge selon l'état réseau actuel
    updateBadge();

    // Charger les photos en attente pour la page bon courante
    const match = window.location.pathname.match(/\/bons\/(\d+)$/);
    if (match) {
      const bonId = parseInt(match[1]);
      try {
        const pending = await pendingPhotosGetAll(bonId);
        for (const p of pending) {
          appendPendingPhotoCard(p.id, p.dataUrl, p.filename);
        }
      } catch (_) {}
    }

    // Tenter une sync si on est en ligne, et garder le planning à jour
    // en cache pour la consultation hors-ligne.
    if (navigator.onLine) {
      syncAll();
      precacherPlanning();
    }
  });

})();
