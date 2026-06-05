/**
 * HPS3D — Gestionnaire hors-ligne
 *
 * Fonctionnement :
 *  1. Quand offline : les photos sont stockées localement (IndexedDB)
 *     avec un badge "En attente" visible dans le bon.
 *  2. Quand online  : synchronisation automatique → envoi au serveur,
 *     les badges "En attente" disparaissent.
 *
 * Exposed globals : pendingPhotosAdd, appendPendingPhotoCard, showToast, updateBadge
 */

(function () {
  'use strict';

  const DB_NAME    = 'hps3d-offline';
  const DB_VERSION = 1;

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
      };
      req.onsuccess = e => resolve(e.target.result);
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
    // Fallback : récupérer un token frais depuis le serveur
    try {
      const resp = await fetch('/api/csrf-token');
      const data = await resp.json();
      return data.csrf_token;
    } catch (_) {
      return '';
    }
  }

  // ── Synchronisation ──────────────────────────────────────────

  let _syncing = false;

  async function syncAll() {
    if (_syncing || !navigator.onLine) return;
    _syncing = true;
    updateBadge();

    try {
      const pending = await pendingPhotosGetAll(null);
      if (!pending.length) { _syncing = false; updateBadge(); return; }

      const csrf = await getCsrfToken();
      let syncedCount = 0;

      for (const photo of pending) {
        try {
          const resp = await fetch('/api/bons/' + photo.bonId + '/photos', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-CSRFToken':  csrf,
            },
            credentials: 'include',
            body: JSON.stringify({
              nom:      photo.filename,
              data:     photo.dataUrl,
              mimetype: photo.mimetype,
            }),
          });

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

    let pending = [];
    try { pending = await pendingPhotosGetAll(null); } catch (_) {}

    if (pending.length > 0) {
      badge.innerHTML = '<i class="bi bi-cloud-upload me-1"></i>' + pending.length + ' photo(s) en attente';
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

  window.addEventListener('online',  () => { updateBadge(); syncAll(); });
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

    // Tenter une sync si on est en ligne
    if (navigator.onLine) syncAll();
  });

})();
