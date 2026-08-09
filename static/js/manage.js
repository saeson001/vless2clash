// ===== Admin Management Script =====

let currentPage = 1;
let totalPages = 1;
let perPage = 20;
let deleteTargetId = null;

// ===== Init =====

(function init() {
    if (ADMIN_LOGGED_IN) {
        showDashboard();
    } else {
        showLogin();
    }
})();

// ===== View Switching =====

function showLogin() {
    document.getElementById('login-view').style.display = 'block';
    document.getElementById('dashboard-view').style.display = 'none';
    // Reset login button state (in case it was stuck at "登录中...")
    const btn = document.getElementById('login-btn');
    if (btn) {
        btn.disabled = false;
        btn.innerHTML = '<span class="btn-icon">🔑</span> 登录';
    }
    // Clear any previous error message
    const errEl = document.getElementById('login-error');
    if (errEl) errEl.style.display = 'none';
    // Auto-focus username
    setTimeout(() => {
        const userInput = document.getElementById('login-username');
        if (userInput) userInput.focus();
    }, 100);
}

function showDashboard() {
    document.getElementById('login-view').style.display = 'none';
    document.getElementById('dashboard-view').style.display = 'block';
    loadStats();
    loadRecords();
}

// ===== Login / Logout =====

function doLogin() {
    const username = document.getElementById('login-username').value.trim();
    const password = document.getElementById('login-password').value;

    if (!username || !password) {
        showLoginError('请输入用户名和密码');
        return;
    }

    const btn = document.getElementById('login-btn');
    btn.disabled = true;
    btn.textContent = '登录中...';

    fetch('/api/admin/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ username, password })
    })
    .then(resp => resp.json().then(data => ({ ok: resp.ok, data })))
    .then(({ ok, data }) => {
        if (ok && data.success) {
            // Verify the session cookie was actually set by calling /api/admin/check
            // This catches cases where gunicorn worker mismatch invalidates the session
            fetch('/api/admin/check', { credentials: 'same-origin' })
                .then(r => r.json())
                .then(checkData => {
                    if (checkData.logged_in) {
                        showDashboard();
                    } else {
                        // Session wasn't established — likely a server-side issue
                        showLoginError('登录失败：会话未建立，请重试');
                        btn.disabled = false;
                        btn.innerHTML = '<span class="btn-icon">🔑</span> 登录';
                    }
                })
                .catch(() => {
                    // If check fails, still try to show dashboard (best effort)
                    showDashboard();
                });
        } else {
            showLoginError(data.error || '登录失败');
            btn.disabled = false;
            btn.innerHTML = '<span class="btn-icon">🔑</span> 登录';
        }
    })
    .catch(() => {
        showLoginError('网络错误，请重试');
        btn.disabled = false;
        btn.innerHTML = '<span class="btn-icon">🔑</span> 登录';
    });
}

function doLogout() {
    fetch('/api/admin/logout', { method: 'POST', credentials: 'same-origin' })
    .then(() => {
        showLogin();
        document.getElementById('login-password').value = '';
    })
    .catch(() => showLogin());
}

function showLoginError(msg) {
    const el = document.getElementById('login-error');
    el.textContent = msg;
    el.style.display = 'block';
}

// Enter key to login
document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && document.getElementById('login-view').style.display !== 'none') {
        doLogin();
    }
});

// ===== Stats =====

function loadStats() {
    fetch('/api/admin/stats', { credentials: 'same-origin' })
    .then(r => r.json())
    .then(data => {
        if (data.error) {
            if (data.error === '未授权') {
                showLogin();
            }
            return;
        }
        document.getElementById('stat-total-records').textContent = data.total_records;
        document.getElementById('stat-total-nodes').textContent = data.total_nodes;
        document.getElementById('stat-unique-ips').textContent = data.unique_ips;
        document.getElementById('stat-recent-24h').textContent = data.recent_24h;

        // Top IPs
        if (data.top_ips && data.top_ips.length > 0) {
            const list = document.getElementById('top-ips-list');
            list.innerHTML = data.top_ips.map(ip => `
                <div class="top-ip-row">
                    <span class="top-ip-addr">${escapeHtml(ip.ip)}</span>
                    <span class="top-ip-count">${ip.count} 次</span>
                    <span class="top-ip-time">${formatTime(ip.last_seen)}</span>
                </div>
            `).join('');
            document.getElementById('top-ips-card').style.display = 'block';
        }
    })
    .catch(() => {});
}

// ===== Records Table =====

function loadRecords() {
    const search = document.getElementById('search-input').value.trim();
    const tbody = document.getElementById('records-tbody');
    tbody.innerHTML = '<tr><td colspan="7" class="table-empty">加载中...</td></tr>';

    fetch(`/api/admin/records?page=${currentPage}&per_page=${perPage}&search=${encodeURIComponent(search)}`, {
        credentials: 'same-origin'
    })
    .then(r => {
        if (r.status === 401) {
            showLogin();
            throw new Error('未授权');
        }
        return r.json();
    })
    .then(data => {
        if (data.error) {
            tbody.innerHTML = `<tr><td colspan="7" class="table-empty error-text">${escapeHtml(data.error)}</td></tr>`;
            return;
        }

        totalPages = data.total_pages;
        document.getElementById('page-info').textContent = `第 ${data.page} / ${totalPages} 页 (共 ${data.total} 条)`;

        // Pagination buttons
        document.getElementById('prev-page').disabled = data.page <= 1;
        document.getElementById('next-page').disabled = data.page >= totalPages;

        if (data.records.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" class="table-empty">暂无记录</td></tr>';
            return;
        }

        tbody.innerHTML = data.records.map(r => `
            <tr>
                <td>${r.id}</td>
                <td class="cell-time">${formatTime(r.created_at)}</td>
                <td class="cell-links">${truncate(escapeHtml(r.original_links || '(订阅转换)'), 60)}</td>
                <td class="cell-ip">${escapeHtml(r.client_ip)}</td>
                <td>${r.node_count}</td>
                <td class="cell-token">${escapeHtml(r.token)}</td>
                <td class="cell-actions">
                    <button class="btn btn-small" onclick="showDetail(${r.id})">详情</button>
                    <button class="btn btn-small btn-danger" onclick="askDelete(${r.id})">删除</button>
                </td>
            </tr>
        `).join('');
    })
    .catch((err) => {
        if (err.message !== '未授权') {
            tbody.innerHTML = '<tr><td colspan="7" class="table-empty error-text">加载失败</td></tr>';
        }
    });
}

function clearFilters() {
    document.getElementById('search-input').value = '';
    currentPage = 1;
    loadRecords();
}

function changePage(delta) {
    const newPage = currentPage + delta;
    if (newPage < 1 || newPage > totalPages) return;
    currentPage = newPage;
    loadRecords();
}

// ===== Record Detail =====

function showDetail(id) {
    fetch(`/api/admin/records/${id}`, { credentials: 'same-origin' })
    .then(r => {
        if (r.status === 401) {
            showLogin();
            throw new Error('未授权');
        }
        return r.json();
    })
    .then(data => {
        if (data.error) {
            alert(data.error);
            return;
        }
        document.getElementById('detail-id').textContent = data.id;
        document.getElementById('detail-time').textContent = formatTime(data.created_at);
        document.getElementById('detail-ip').textContent = data.client_ip;
        document.getElementById('detail-nodes').textContent = data.node_count + ' 个';
        document.getElementById('detail-token').textContent = data.token;

        // Download link
        const linkEl = document.getElementById('detail-download-link');
        if (data.download_url) {
            linkEl.href = data.download_url;
            linkEl.textContent = data.download_url;
        } else {
            linkEl.href = '#';
            linkEl.textContent = '(无)';
        }

        document.getElementById('detail-subscriptions').textContent = data.subscription_urls || '(无)';
        document.getElementById('detail-original').textContent = data.original_links || '(无)';
        document.getElementById('detail-yaml').textContent = data.yaml_content || '(无)';
        document.getElementById('detail-modal').style.display = 'flex';
    })
    .catch((err) => {
        if (err.message !== '未授权') {
            alert('加载详情失败');
        }
    });
}

function closeDetailModal() {
    document.getElementById('detail-modal').style.display = 'none';
}

function copyDownloadLink() {
    const linkEl = document.getElementById('detail-download-link');
    const url = linkEl.textContent;
    if (!url || url === '(无)') return;

    const btn = document.getElementById('copy-download-btn');
    const originalHTML = btn.innerHTML;

    // Same fallback logic as the main app's copyToClipboard
    if (window.isSecureContext && navigator.clipboard) {
        navigator.clipboard.writeText(url).then(() => {
            btn.innerHTML = '<span class="btn-icon">✅</span> 已复制';
            setTimeout(() => { btn.innerHTML = originalHTML; }, 1500);
        }).catch(() => {
            fallbackCopy(url, btn, originalHTML);
        });
    } else {
        fallbackCopy(url, btn, originalHTML);
    }
}

function fallbackCopy(text, btn, originalHTML) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    try {
        document.execCommand('copy');
        btn.innerHTML = '<span class="btn-icon">✅</span> 已复制';
    } catch {
        btn.innerHTML = '<span class="btn-icon">❌</span> 失败';
    }
    document.body.removeChild(ta);
    setTimeout(() => { btn.innerHTML = originalHTML; }, 1500);
}

// ===== Delete Record =====

function askDelete(id) {
    deleteTargetId = id;
    document.getElementById('delete-modal').style.display = 'flex';
}

function closeDeleteModal() {
    document.getElementById('delete-modal').style.display = 'none';
    deleteTargetId = null;
}

function confirmDelete() {
    if (!deleteTargetId) return;

    const btn = document.getElementById('confirm-delete-btn');
    btn.disabled = true;
    btn.textContent = '删除中...';

    fetch(`/api/admin/records/${deleteTargetId}`, {
        method: 'DELETE',
        credentials: 'same-origin'
    })
    .then(r => {
        if (r.status === 401) {
            showLogin();
            throw new Error('未授权');
        }
        return r.json();
    })
    .then(data => {
        if (data.success) {
            closeDeleteModal();
            loadRecords();
            loadStats();
        } else {
            alert(data.error || '删除失败');
        }
    })
    .catch((err) => {
        if (err.message !== '未授权') {
            alert('删除失败');
        }
    })
    .finally(() => {
        btn.disabled = false;
        btn.textContent = '确认删除';
    });
}

// ===== Change Password =====

function showChangePassword() {
    document.getElementById('old-password').value = '';
    document.getElementById('new-password').value = '';
    document.getElementById('confirm-password').value = '';
    document.getElementById('password-error').style.display = 'none';
    document.getElementById('password-modal').style.display = 'flex';
}

function closePasswordModal() {
    document.getElementById('password-modal').style.display = 'none';
}

function submitChangePassword() {
    const oldPwd = document.getElementById('old-password').value;
    const newPwd = document.getElementById('new-password').value;
    const confirmPwd = document.getElementById('confirm-password').value;

    if (!oldPwd || !newPwd || !confirmPwd) {
        showPasswordError('请填写所有字段');
        return;
    }

    if (newPwd !== confirmPwd) {
        showPasswordError('两次输入的新密码不一致');
        return;
    }

    if (newPwd.length < 8) {
        showPasswordError('新密码至少 8 个字符');
        return;
    }

    fetch('/api/admin/change-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ old_password: oldPwd, new_password: newPwd })
    })
    .then(r => {
        if (r.status === 401) {
            showLogin();
            throw new Error('未授权');
        }
        return r.json();
    })
    .then(data => {
        if (data.success) {
            closePasswordModal();
            alert('密码修改成功，请重新登录');
            doLogout();
        } else {
            showPasswordError(data.error || '修改失败');
        }
    })
    .catch((err) => {
        if (err.message !== '未授权') {
            showPasswordError('网络错误');
        }
    });
}

function showPasswordError(msg) {
    const el = document.getElementById('password-error');
    el.textContent = msg;
    el.style.display = 'block';
}

// ===== Utilities =====

function formatTime(isoStr) {
    if (!isoStr) return '-';
    try {
        const d = new Date(isoStr);
        return d.getFullYear() + '-' +
               String(d.getMonth() + 1).padStart(2, '0') + '-' +
               String(d.getDate()).padStart(2, '0') + ' ' +
               String(d.getHours()).padStart(2, '0') + ':' +
               String(d.getMinutes()).padStart(2, '0') + ':' +
               String(d.getSeconds()).padStart(2, '0');
    } catch {
        return isoStr;
    }
}

function truncate(str, maxLen) {
    if (str.length <= maxLen) return str;
    return str.substring(0, maxLen) + '...';
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Close modals on Escape
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        closeDetailModal();
        closePasswordModal();
        closeDeleteModal();
    }
});
