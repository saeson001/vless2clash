// ===== Admin Management Script =====

let currentPage = 1;
let totalPages = 1;
let perPage = 20;
let deleteTargetId = null;

// ===== Collapse State Persistence =====

const COLLAPSE_STORAGE_KEY = 'vless2clash_panel_states';

function getCollapseStates() {
    try {
        return JSON.parse(localStorage.getItem(COLLAPSE_STORAGE_KEY)) || {};
    } catch {
        return {};
    }
}

function setCollapseState(panelId, collapsed) {
    const states = getCollapseStates();
    states[panelId] = collapsed;
    try {
        localStorage.setItem(COLLAPSE_STORAGE_KEY, JSON.stringify(states));
    } catch {}
}

function togglePanel(panelId) {
    const content = document.getElementById(panelId + '-content');
    const arrow = document.getElementById(panelId + '-arrow');
    if (!content || !arrow) return;

    const isCollapsed = content.style.display === 'none';
    if (isCollapsed) {
        content.style.display = 'block';
        arrow.textContent = '▼';
        setCollapseState(panelId, false);
    } else {
        content.style.display = 'none';
        arrow.textContent = '▶';
        setCollapseState(panelId, true);
    }
}

function applyStoredCollapseStates(callback) {
    const states = getCollapseStates();
    ['top-ips', 'daily'].forEach(function(id) {
        const content = document.getElementById(id + '-content');
        const arrow = document.getElementById(id + '-arrow');
        if (!content || !arrow) return;

        // Default: collapsed (display:none)
        var collapsed = states[id];
        if (collapsed === undefined || collapsed === null) {
            collapsed = true; // default collapsed
        }

        if (collapsed) {
            content.style.display = 'none';
            arrow.textContent = '▶';
        } else {
            content.style.display = 'block';
            arrow.textContent = '▼';
        }
    });
    if (callback) callback();
}

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
    setTimeout(function() {
        const userInput = document.getElementById('login-username');
        if (userInput) userInput.focus();
    }, 100);
}

function showDashboard() {
    document.getElementById('login-view').style.display = 'none';
    document.getElementById('dashboard-view').style.display = 'block';
    loadStats();
    loadDailyStats();
    loadRecords();
    // Apply stored collapse states after a short delay
    // to ensure panels exist in DOM
    setTimeout(function() {
        applyStoredCollapseStates();
    }, 50);
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
        body: JSON.stringify({ username: username, password: password })
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (result.ok && result.data.success) {
            // Verify the session cookie was actually set by calling /api/admin/check
            fetch('/api/admin/check', { credentials: 'same-origin' })
                .then(function(r) { return r.json(); })
                .then(function(checkData) {
                    if (checkData.logged_in) {
                        showDashboard();
                    } else {
                        showLoginError('登录失败：会话未建立，请重试');
                        btn.disabled = false;
                        btn.innerHTML = '<span class="btn-icon">🔑</span> 登录';
                    }
                })
                .catch(function() {
                    showDashboard();
                });
        } else {
            showLoginError(result.data.error || '登录失败');
            btn.disabled = false;
            btn.innerHTML = '<span class="btn-icon">🔑</span> 登录';
        }
    })
    .catch(function() {
        showLoginError('网络错误，请重试');
        btn.disabled = false;
        btn.innerHTML = '<span class="btn-icon">🔑</span> 登录';
    });
}

function doLogout() {
    fetch('/api/admin/logout', { method: 'POST', credentials: 'same-origin' })
    .then(function() {
        showLogin();
        document.getElementById('login-password').value = '';
    })
    .catch(function() { showLogin(); });
}

function showLoginError(msg) {
    const el = document.getElementById('login-error');
    el.textContent = msg;
    el.style.display = 'block';
}

// Enter key to login
document.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && document.getElementById('login-view').style.display !== 'none') {
        doLogin();
    }
});

// ===== Stats =====

function loadStats() {
    fetch('/api/admin/stats', { credentials: 'same-origin' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
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
        var list = document.getElementById('top-ips-list');
        if (data.top_ips && data.top_ips.length > 0) {
            list.innerHTML = data.top_ips.map(function(ip) {
                return '<div class="top-ip-row">' +
                    '<span class="top-ip-addr">' + escapeHtml(ip.ip) + '</span>' +
                    '<span class="top-ip-count">' + ip.count + ' 次</span>' +
                    '<span class="top-ip-time">' + formatTime(ip.last_seen) + '</span>' +
                '</div>';
            }).join('');
            document.getElementById('top-ips-card').style.display = 'block';
        } else {
            document.getElementById('top-ips-card').style.display = 'none';
        }
    })
    .catch(function() {});
}

// ===== Daily Stats =====

function loadDailyStats() {
    fetch('/api/admin/daily-stats?days=7', { credentials: 'same-origin' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.error) {
            if (data.error === '未授权') {
                showLogin();
            }
            return;
        }
        document.getElementById('stat-today-count').textContent = data.today_count;
        document.getElementById('stat-week-count').textContent = data.week_count;

        // Render daily breakdown table
        var tbody = document.getElementById('daily-tbody');
        if (!data.daily || data.daily.length === 0) {
            tbody.innerHTML = '<tr><td colspan="4" class="table-empty">暂无数据</td></tr>';
            return;
        }

        // Find max count for visual bar
        var maxCount = Math.max.apply(null, [1].concat(data.daily.map(function(d) { return d.count; })));

        tbody.innerHTML = data.daily.map(function(d) {
            var barWidth = Math.round((d.count / maxCount) * 100);
            var barColor = d.is_today ? '#4c6ef5' : '#6c757d';
            return '<tr class="' + (d.is_today ? 'daily-row-today' : '') + '">' +
                '<td>' + formatDate(d.date) + '</td>' +
                '<td>' +
                    '<span class="daily-count">' + d.count + ' 次</span>' +
                    '<div class="daily-bar-bg">' +
                        '<div class="daily-bar" style="width:' + barWidth + '%;background:' + barColor + ';"></div>' +
                    '</div>' +
                '</td>' +
                '<td>' + d.nodes + ' 个</td>' +
                '<td>' + (d.is_today ? '<span class="badge badge-today">今天</span>' : '') + '</td>' +
            '</tr>';
        }).join('');

        // Re-apply collapse state after rendering
        applyStoredCollapseStates();
    })
    .catch(function() {});
}

// ===== Records Table =====

function loadRecords() {
    var search = document.getElementById('search-input').value.trim();
    var tbody = document.getElementById('records-tbody');
    tbody.innerHTML = '<tr><td colspan="7" class="table-empty">加载中...</td></tr>';

    fetch('/api/admin/records?page=' + currentPage + '&per_page=' + perPage + '&search=' + encodeURIComponent(search), {
        credentials: 'same-origin'
    })
    .then(function(r) {
        if (r.status === 401) {
            showLogin();
            throw new Error('未授权');
        }
        return r.json();
    })
    .then(function(data) {
        if (data.error) {
            tbody.innerHTML = '<tr><td colspan="7" class="table-empty error-text">' + escapeHtml(data.error) + '</td></tr>';
            return;
        }

        totalPages = data.total_pages;
        document.getElementById('page-info').textContent =
            '第 ' + data.page + ' / ' + totalPages + ' 页 (共 ' + data.total + ' 条)';

        // Pagination buttons
        document.getElementById('prev-page').disabled = data.page <= 1;
        document.getElementById('next-page').disabled = data.page >= totalPages;

        if (data.records.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" class="table-empty">暂无记录</td></tr>';
            return;
        }

        tbody.innerHTML = data.records.map(function(r) {
            var updateCountClass = (r.update_count >= 5) ? ' class="update-count-high"' : '';
            return '<tr>' +
                '<td>' + r.id + '</td>' +
                '<td class="cell-time">' + formatTime(r.created_at) + '</td>' +
                '<td class="cell-ip">' + escapeHtml(r.client_ip) + '</td>' +
                '<td>' + r.node_count + '</td>' +
                '<td class="cell-token">' + escapeHtml(r.token) + '</td>' +
                '<td' + updateCountClass + '>' + r.update_count + '</td>' +
                '<td class="cell-actions">' +
                    '<button class="btn btn-small" onclick="showDetail(' + r.id + ')">详情</button>' +
                    '<button class="btn btn-small" onclick="copyRecordLink(\'' + escapeAttr(r.token) + '\', this)">复制</button>' +
                    '<button class="btn btn-small" onclick="showEdit(' + r.id + ')">编辑</button>' +
                    '<button class="btn btn-small btn-refresh" onclick="askRefresh(' + r.id + ')">更新</button>' +
                    '<button class="btn btn-small btn-danger" onclick="askDelete(' + r.id + ')">删除</button>' +
                '</td>' +
            '</tr>';
        }).join('');
    })
    .catch(function(err) {
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
    var newPage = currentPage + delta;
    if (newPage < 1 || newPage > totalPages) return;
    currentPage = newPage;
    loadRecords();
}

// ===== Record Detail =====

function showDetail(id) {
    fetch('/api/admin/records/' + id, { credentials: 'same-origin' })
    .then(function(r) {
        if (r.status === 401) {
            showLogin();
            throw new Error('未授权');
        }
        return r.json();
    })
    .then(function(data) {
        if (data.error) {
            alert(data.error);
            return;
        }
        document.getElementById('detail-id').textContent = data.id;
        document.getElementById('detail-time').textContent = formatTime(data.created_at);
        document.getElementById('detail-ip').textContent = data.client_ip;
        document.getElementById('detail-ip-update-count').textContent = data.ip_update_count + ' 次';
        document.getElementById('detail-nodes').textContent = data.node_count + ' 个';
        document.getElementById('detail-token').textContent = data.token;
        document.getElementById('detail-config-name').textContent = data.config_name || '(自动)';

        // Download link
        var linkEl = document.getElementById('detail-download-link');
        if (data.download_url) {
            linkEl.href = data.download_url;
            linkEl.textContent = data.download_url;
        } else {
            linkEl.href = '#';
            linkEl.textContent = '(无)';
        }

        // IP Stats Panel
        var statsSection = document.getElementById('detail-ip-stats-section');
        var statsPanel = document.getElementById('detail-ip-stats');
        if (data.top_ips && data.top_ips.length > 0) {
            statsSection.style.display = 'block';
            statsPanel.innerHTML = data.top_ips.map(function(ip) {
                var highlight = (ip.ip === data.client_ip) ? ' ip-stats-row-active' : '';
                return '<div class="ip-stats-row' + highlight + '">' +
                    '<span class="ip-stats-addr">' + escapeHtml(ip.ip) + '</span>' +
                    '<span class="ip-stats-count">' + ip.count + ' 次</span>' +
                    '<span class="ip-stats-time">' + formatTime(ip.last_seen) + '</span>' +
                '</div>';
            }).join('');
        } else {
            statsSection.style.display = 'none';
        }

        document.getElementById('detail-subscriptions').textContent = data.subscription_urls || '(无)';
        document.getElementById('detail-original').textContent = data.original_links || '(无)';
        document.getElementById('detail-yaml').textContent = data.yaml_content || '(无)';
        document.getElementById('detail-modal').style.display = 'flex';
    })
    .catch(function(err) {
        if (err.message !== '未授权') {
            alert('加载详情失败');
        }
    });
}

function closeDetailModal() {
    document.getElementById('detail-modal').style.display = 'none';
}

function copyDownloadLink() {
    var linkEl = document.getElementById('detail-download-link');
    var url = linkEl.textContent;
    if (!url || url === '(无)') return;

    var btn = document.getElementById('copy-download-btn');
    var originalHTML = btn.innerHTML;

    // Same fallback logic as the main app's copyToClipboard
    if (window.isSecureContext && navigator.clipboard) {
        navigator.clipboard.writeText(url).then(function() {
            btn.innerHTML = '<span class="btn-icon">✅</span> 已复制';
            setTimeout(function() { btn.innerHTML = originalHTML; }, 1500);
        }).catch(function() {
            fallbackCopy(url, btn, originalHTML);
        });
    } else {
        fallbackCopy(url, btn, originalHTML);
    }
}

function fallbackCopy(text, btn, originalHTML) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    try {
        document.execCommand('copy');
        btn.innerHTML = '<span class="btn-icon">✅</span> 已复制';
    } catch(e) {
        btn.innerHTML = '<span class="btn-icon">❌</span> 失败';
    }
    document.body.removeChild(ta);
    setTimeout(function() { btn.innerHTML = originalHTML; }, 1500);
}

// Copy download link from records table (by token)
function copyRecordLink(token, btn) {
    var url = window.location.origin + '/d/' + token;
    var originalHTML = btn.innerHTML;

    if (window.isSecureContext && navigator.clipboard) {
        navigator.clipboard.writeText(url).then(function() {
            btn.innerHTML = '✅ 已复制';
            setTimeout(function() { btn.innerHTML = originalHTML; }, 1500);
        }).catch(function() {
            fallbackCopy(url, btn, originalHTML);
        });
    } else {
        fallbackCopy(url, btn, originalHTML);
    }
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

    var btn = document.getElementById('confirm-delete-btn');
    btn.disabled = true;
    btn.textContent = '删除中...';

    fetch('/api/admin/records/' + deleteTargetId, {
        method: 'DELETE',
        credentials: 'same-origin'
    })
    .then(function(r) {
        if (r.status === 401) {
            showLogin();
            throw new Error('未授权');
        }
        return r.json();
    })
    .then(function(data) {
        if (data.success) {
            closeDeleteModal();
            loadRecords();
            loadStats();
        } else {
            alert(data.error || '删除失败');
        }
    })
    .catch(function(err) {
        if (err.message !== '未授权') {
            alert('删除失败');
        }
    })
    .finally(function() {
        btn.disabled = false;
        btn.textContent = '确认删除';
    });
}

// ===== Edit Record =====

var editTargetId = null;

function showEdit(id) {
    editTargetId = id;
    document.getElementById('edit-error').style.display = 'none';

    // Fetch record detail to pre-fill the form
    fetch('/api/admin/records/' + id, { credentials: 'same-origin' })
    .then(function(r) {
        if (r.status === 401) {
            showLogin();
            throw new Error('未授权');
        }
        return r.json();
    })
    .then(function(data) {
        if (data.error) {
            alert(data.error);
            return;
        }
        document.getElementById('edit-id').textContent = data.id;
        document.getElementById('edit-token').textContent = data.token;
        document.getElementById('edit-config-name').value = data.config_name || '';
        document.getElementById('edit-links').value = data.original_links || '';
        document.getElementById('edit-subscriptions').value = data.subscription_urls || '';
        // Default to basic when editing
        document.getElementById('edit-rules-mode').value = 'basic';
        document.getElementById('edit-modal').style.display = 'flex';
    })
    .catch(function(err) {
        if (err.message !== '未授权') {
            alert('加载记录失败');
        }
    });
}

function closeEditModal() {
    document.getElementById('edit-modal').style.display = 'none';
    editTargetId = null;
}

function submitEdit() {
    if (!editTargetId) return;

    var links = document.getElementById('edit-links').value.trim();
    var subscriptions = document.getElementById('edit-subscriptions').value.trim();
    var configName = document.getElementById('edit-config-name').value.trim();
    var rulesMode = document.getElementById('edit-rules-mode').value;

    if (!links && !subscriptions) {
        showEditError('请输入代理链接或订阅地址');
        return;
    }

    var btn = document.getElementById('submit-edit-btn');
    btn.disabled = true;
    btn.textContent = '更新中...';

    fetch('/api/admin/records/' + editTargetId + '/edit', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({
            links: links,
            subscriptions: subscriptions,
            config_name: configName,
            rules_mode: rulesMode
        })
    })
    .then(function(r) {
        if (r.status === 401) {
            showLogin();
            throw new Error('未授权');
        }
        return r.json();
    })
    .then(function(data) {
        if (data.success) {
            closeEditModal();
            loadRecords();
            loadStats();
            if (data.errors && data.errors.length > 0) {
                alert('更新成功，但有部分错误:\n' + data.errors.join('\n'));
            }
        } else {
            showEditError(data.error || '更新失败');
        }
    })
    .catch(function(err) {
        if (err.message !== '未授权') {
            showEditError('网络错误');
        }
    })
    .finally(function() {
        btn.disabled = false;
        btn.textContent = '保存更新';
    });
}

function showEditError(msg) {
    var el = document.getElementById('edit-error');
    el.textContent = msg;
    el.style.display = 'block';
}

// ===== Refresh Record (one-click update) =====

var refreshTargetId = null;

function askRefresh(id) {
    refreshTargetId = id;
    document.getElementById('refresh-error').style.display = 'none';
    document.getElementById('refresh-rules-mode').value = 'basic';
    document.getElementById('refresh-modal').style.display = 'flex';
}

function closeRefreshModal() {
    document.getElementById('refresh-modal').style.display = 'none';
    refreshTargetId = null;
}

function confirmRefresh() {
    if (!refreshTargetId) return;

    var rulesMode = document.getElementById('refresh-rules-mode').value;
    var btn = document.getElementById('confirm-refresh-btn');
    btn.disabled = true;
    btn.textContent = '刷新中...';

    fetch('/api/admin/records/' + refreshTargetId + '/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ rules_mode: rulesMode })
    })
    .then(function(r) {
        if (r.status === 401) {
            showLogin();
            throw new Error('未授权');
        }
        return r.json();
    })
    .then(function(data) {
        if (data.success) {
            closeRefreshModal();
            loadRecords();
            loadStats();
            alert(data.message);
        } else {
            var errEl = document.getElementById('refresh-error');
            errEl.textContent = data.error || '刷新失败';
            errEl.style.display = 'block';
        }
    })
    .catch(function(err) {
        if (err.message !== '未授权') {
            var errEl = document.getElementById('refresh-error');
            errEl.textContent = '网络错误';
            errEl.style.display = 'block';
        }
    })
    .finally(function() {
        btn.disabled = false;
        btn.textContent = '确认刷新';
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
    var oldPwd = document.getElementById('old-password').value;
    var newPwd = document.getElementById('new-password').value;
    var confirmPwd = document.getElementById('confirm-password').value;

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
    .then(function(r) {
        if (r.status === 401) {
            showLogin();
            throw new Error('未授权');
        }
        return r.json();
    })
    .then(function(data) {
        if (data.success) {
            closePasswordModal();
            alert('密码修改成功，请重新登录');
            doLogout();
        } else {
            showPasswordError(data.error || '修改失败');
        }
    })
    .catch(function(err) {
        if (err.message !== '未授权') {
            showPasswordError('网络错误');
        }
    });
}

function showPasswordError(msg) {
    var el = document.getElementById('password-error');
    el.textContent = msg;
    el.style.display = 'block';
}

// ===== Utilities =====

function formatTime(isoStr) {
    if (!isoStr) return '-';
    try {
        var d = new Date(isoStr);
        return d.getFullYear() + '-' +
               pad2(d.getMonth() + 1) + '-' +
               pad2(d.getDate()) + ' ' +
               pad2(d.getHours()) + ':' +
               pad2(d.getMinutes()) + ':' +
               pad2(d.getSeconds());
    } catch(e) {
        return isoStr;
    }
}

function pad2(n) {
    return (n < 10 ? '0' : '') + n;
}

function formatDate(dateStr) {
    if (!dateStr) return '-';
    try {
        var parts = dateStr.split('-');
        var weekdays = ['日', '一', '二', '三', '四', '五', '六'];
        var d = new Date(parseInt(parts[0]), parseInt(parts[1]) - 1, parseInt(parts[2]));
        return parts[1] + '月' + parts[2] + '日 (周' + weekdays[d.getDay()] + ')';
    } catch(e) {
        return dateStr;
    }
}

function truncate(str, maxLen) {
    if (str.length <= maxLen) return str;
    return str.substring(0, maxLen) + '...';
}

function escapeHtml(text) {
    var div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function escapeAttr(text) {
    return text.replace(/'/g, "\\'");
}

// Close modals on Escape
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        closeDetailModal();
        closePasswordModal();
        closeDeleteModal();
        closeEditModal();
        closeRefreshModal();
    }
});
