// ===== UI Toggle Functions =====

// Fetch and display the current version on page load
(function loadVersion() {
    fetch('/api/version')
        .then(r => r.json())
        .then(data => {
            const badge = document.getElementById('version-badge');
            if (badge && data.version) badge.textContent = data.version;
        })
        .catch(() => {});
})();

function toggleSubscription() {
    const section = document.getElementById('subscription-section');
    const arrow = document.getElementById('sub-arrow');
    if (section.style.display === 'none') {
        section.style.display = 'block';
        arrow.style.transform = 'rotate(180deg)';
    } else {
        section.style.display = 'none';
        arrow.style.transform = 'rotate(0deg)';
    }
}

function toggleConfig() {
    const section = document.getElementById('config-section');
    const arrow = document.getElementById('config-arrow');
    if (section.style.display === 'none') {
        section.style.display = 'block';
        arrow.style.transform = 'rotate(180deg)';
    } else {
        section.style.display = 'none';
        arrow.style.transform = 'rotate(0deg)';
    }
}

// ===== Main Actions =====

function convert(aiOverride) {
    const links = document.getElementById('vless-input').value.trim();
    const subscriptions = document.getElementById('subscription-input') ? document.getElementById('subscription-input').value.trim() : '';
    const config = getConfig();
    const configName = document.getElementById('cfg-name') ? document.getElementById('cfg-name').value.trim() : '';

    if (!links && !subscriptions) {
        showStatus('请输入代理链接或订阅地址', 'error');
        return;
    }

    const payload = { links, subscriptions, config, config_name: configName };
    // When the picker supplied explicit nodes, force AI routing on with them
    if (aiOverride) {
        payload.ai_routing = true;
        payload.ai_japan = aiOverride.japan;
        payload.ai_hongkong = aiOverride.hongkong;
    }

    showStatus('正在转换...', 'loading');
    document.getElementById('convert-btn').disabled = true;

    fetch('/api/convert', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    })
    .then(resp => {
        if (!resp.ok) {
            return resp.json().then(data => { throw new Error(data.error || '转换失败'); });
        }
        return resp.json();
    })
    .then(data => {
        if (data.ai_routing_ambiguous) {
            showAiPicker(data);
            return;
        }
        document.getElementById('status-bar').style.display = 'none';
        showResult(data);
    })
    .catch(err => {
        showStatus(err.message, 'error');
    })
    .finally(() => {
        document.getElementById('convert-btn').disabled = false;
    });
}

function showAiPicker(data) {
    const jp = document.getElementById('ai-japan-select');
    const hk = document.getElementById('ai-hk-select');
    jp.innerHTML = '';
    hk.innerHTML = '';
    (data.candidates || []).forEach(name => {
        jp.appendChild(new Option(name, name));
        hk.appendChild(new Option(name, name));
    });

    // Preselect auto-detected node when unambiguous
    if (data.detected_japan && data.detected_japan.length === 1) jp.value = data.detected_japan[0];
    if (data.detected_hongkong && data.detected_hongkong.length === 1) hk.value = data.detected_hongkong[0];

    // Fallback defaults: first candidate as Japan, last as Hong Kong
    if (!jp.value && data.candidates && data.candidates.length) jp.value = data.candidates[0];
    if (!hk.value && data.candidates && data.candidates.length) {
        hk.value = data.candidates[data.candidates.length - 1];
    }

    document.getElementById('ai-picker').style.display = 'block';
    document.getElementById('ai-picker').scrollIntoView({ behavior: 'smooth', block: 'start' });
    showStatus('请指定日本 / 香港节点后点「确认生成」', 'loading');
}

function confirmAiRouting() {
    const japan = document.getElementById('ai-japan-select').value;
    const hongkong = document.getElementById('ai-hk-select').value;
    document.getElementById('ai-picker').style.display = 'none';
    convert({ japan, hongkong });
}

function cancelAiRouting() {
    document.getElementById('ai-picker').style.display = 'none';
}

function showResult(data) {
    // Show YAML output
    document.getElementById('yaml-output').textContent = data.yaml;
    document.getElementById('result-section').style.display = 'block';

    // Show proxy summary
    const summary = document.getElementById('proxy-summary');
    summary.innerHTML = '';
    data.proxies.forEach(p => {
        const tag = document.createElement('span');
        tag.className = 'proxy-tag';
        tag.textContent = `${p.name} (${p.server}:${p.port})`;
        summary.appendChild(tag);
    });

    // Show download link section
    const dlSection = document.getElementById('download-link-section');
    const fileBadge = document.getElementById('file-badge');
    const urlInput = document.getElementById('download-url-input');
    const dlAnchor = document.getElementById('download-anchor');

    if (data.download_url && data.token) {
        dlSection.style.display = 'block';
        fileBadge.textContent = data.config_name || data.token;

        // Build full URL for display
        const fullUrl = window.location.origin + data.download_url;
        urlInput.value = fullUrl;
        urlInput.onclick = function() { this.select(); };

        dlAnchor.href = data.download_url;
        dlAnchor.download = 'config.yaml';
    } else {
        dlSection.style.display = 'none';
    }

    // Show errors if any
    const errorSection = document.getElementById('error-section');
    const errorList = document.getElementById('error-list');
    if (data.errors && data.errors.length > 0) {
        errorList.innerHTML = data.errors.map(e => `<p>${escapeHtml(e)}</p>`).join('');
        errorSection.style.display = 'block';
    } else {
        errorSection.style.display = 'none';
    }

    // Show status
    showStatus(`转换成功！共 ${data.count} 个节点，下载链接已生成`, 'success');

    // Scroll to result
    document.getElementById('result-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function showStatus(msg, type) {
    const bar = document.getElementById('status-bar');
    bar.style.display = 'flex';
    bar.className = 'status-bar status-' + type;
    bar.textContent = msg;
    if (type !== 'loading') {
        setTimeout(() => { bar.style.display = 'none'; }, 5000);
    }
}

function getConfig() {
    return {
        port: parseInt(document.getElementById('cfg-port').value) || 7890,
        allow_lan: document.getElementById('cfg-allow-lan').value === 'true',
        mode: document.getElementById('cfg-mode').value,
        log_level: document.getElementById('cfg-log-level').value,
        group_name: document.getElementById('cfg-group-name').value || '节点选择',
        rules_mode: document.getElementById('cfg-rules-mode') ? document.getElementById('cfg-rules-mode').value : 'basic',
        ai_routing: document.getElementById('cfg-ai-routing') ? document.getElementById('cfg-ai-routing').value === 'on' : false,
    };
}

// ===== Clipboard (兼容 HTTP 非 HTTPS 环境) =====

function copyToClipboard(text) {
    return new Promise((resolve, reject) => {
        // 方案1: navigator.clipboard (需要 HTTPS 或 localhost)
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(text).then(resolve).catch(() => {
                // clipboard API 失败，降级到方案2
                fallbackCopy(text) ? resolve() : reject();
            });
            return;
        }
        // 方案2: textarea + execCommand (兼容 HTTP 环境)
        fallbackCopy(text) ? resolve() : reject();
    });
}

function fallbackCopy(text) {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    // 放到屏幕外，不可见
    textarea.style.position = 'fixed';
    textarea.style.top = '-9999px';
    textarea.style.left = '-9999px';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    let ok = false;
    try {
        ok = document.execCommand('copy');
    } catch (e) {
        ok = false;
    }
    document.body.removeChild(textarea);
    return ok;
}

function copyResult() {
    const text = document.getElementById('yaml-output').textContent;
    copyToClipboard(text).then(() => {
        showStatus('配置内容已复制到剪贴板', 'success');
    }).catch(() => {
        showStatus('复制失败，请手动选择文本复制', 'error');
    });
}

function copyLink() {
    const urlInput = document.getElementById('download-url-input');
    const url = urlInput.value;
    copyToClipboard(url).then(() => {
        showStatus('下载链接已复制: ' + url, 'success');
    }).catch(() => {
        // 最后兜底：选中输入框让用户手动 Ctrl+C
        urlInput.select();
        showStatus('自动复制失败，请按 Ctrl+C 手动复制', 'error');
    });
}

function loadExample() {
    const example = 'vless://c164c8aa-3db8-49bb-9675-bdbfc2ecdb39@38.47.108.240:57613?type=tcp&encryption=none&security=reality&sni=apple.com&pbk=53eqWPu-fQR8tPXoSc5tLZ1wCgyIExpt04e3ZDMQ2i8&sid=a290181c&fp=chrome#%E5%88%98%E5%BC%A0%E8%88%AA%E7%88%B8%E7%88%B8';
    document.getElementById('vless-input').value = example;
    showStatus('示例已加载，点击「一键转换」', 'success');
}

function clearAll() {
    document.getElementById('vless-input').value = '';
    const subInput = document.getElementById('subscription-input');
    if (subInput) subInput.value = '';
    document.getElementById('result-section').style.display = 'none';
    document.getElementById('error-section').style.display = 'none';
    document.getElementById('status-bar').style.display = 'none';
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ===== Keyboard Shortcut =====

document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        convert();
    }
});
