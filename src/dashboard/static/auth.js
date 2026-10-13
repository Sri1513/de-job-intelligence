/* Job Scout dashboard auth helper.
 * Reads stay public. Write endpoints need the dashboard token, kept in
 * sessionStorage (cleared when the tab closes). Include this file on any
 * page with action buttons, and call djWrite() instead of fetch() for
 * mutating API calls.
 */
(function () {
    'use strict';

    function djToken() {
        try { return sessionStorage.getItem('dj_token') || ''; }
        catch (e) { return ''; }
    }

    function djIsLoggedIn() { return djToken().length > 0; }

    async function djWrite(url, body) {
        const res = await fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + djToken()
            },
            body: JSON.stringify(body === undefined ? {} : body)
        });
        if (res.status === 401) {
            const next = encodeURIComponent(window.location.pathname + window.location.search);
            window.location.href = '/login?next=' + next;
            throw new Error('auth-required');
        }
        return res;
    }

    function djLogout() {
        try { sessionStorage.removeItem('dj_token'); } catch (e) {}
        window.location.reload();
    }

    function djRenderAuthButton() {
        if (document.getElementById('dj-auth-btn')) return;
        const btn = document.createElement('button');
        btn.id = 'dj-auth-btn';
        btn.style.cssText = 'position:fixed;bottom:16px;right:16px;z-index:9999;' +
            'background:#0f172a;border:1px solid #334155;color:#e2e8f0;' +
            'font-size:13px;padding:8px 14px;border-radius:10px;cursor:pointer;';
        if (djIsLoggedIn()) {
            btn.textContent = '🔓 Log out';
            btn.title = 'Logged in — actions enabled on this device tab';
            btn.onclick = djLogout;
        } else {
            btn.textContent = '🔒 Log in';
            btn.title = 'Log in to enable actions (browsing stays public)';
            btn.onclick = function () {
                const next = encodeURIComponent(window.location.pathname + window.location.search);
                window.location.href = '/login?next=' + next;
            };
        }
        document.body.appendChild(btn);
    }

    window.djWrite = djWrite;
    window.djIsLoggedIn = djIsLoggedIn;
    window.djLogout = djLogout;
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', djRenderAuthButton);
    } else {
        djRenderAuthButton();
    }
})();
