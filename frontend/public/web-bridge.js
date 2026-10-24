/**
 * Web 模式的 pywebview 桥接层。
 *
 * 仅当页面由 `backend/web_server.py` 提供时加载（服务端注入 window.__KEYREALM_WEB__）。
 * 它把前端既有的 `window.pywebview.api.<method>(...)` 调用映射到 HTTP RPC，
 * 并把 SSE 事件还原成 `CustomEvent`——前端代码不需要为 Web 模式改任何调用方式。
 */
(function () {
  const cfg = window.__KEYREALM_WEB__;
  if (!cfg || !cfg.token) return;
  if (window.pywebview && window.pywebview.api) return;

  const token = cfg.token;
  const headers = {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${token}`,
  };

  async function post(url, body) {
    const resp = await fetch(url, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
    });
    let data = null;
    try {
      data = await resp.json();
    } catch {
      data = null;
    }
    if (!resp.ok) {
      throw new Error((data && data.message) || `HTTP ${resp.status}`);
    }
    return data;
  }

  function downloadBlob(blob, filename) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename || 'download';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }

  function downloadText(text, filename) {
    downloadBlob(new Blob([text], { type: 'application/json' }), filename || 'download.json');
  }

  // 下载/打包类操作：插件把文件写到服务器临时路径，动作结束后再从这里取回。
  // key=服务器路径，value=浏览器下载时显示的文件名。
  const pendingDownloads = new Map();

  async function downloadPending() {
    for (const [path, name] of Array.from(pendingDownloads.entries())) {
      pendingDownloads.delete(path);
      try {
        const query = new URLSearchParams({ token, path, name });
        const resp = await fetch(`/api/download?${query.toString()}`);
        if (!resp.ok) continue;   // 动作失败/取消时文件不存在，静默跳过
        downloadBlob(await resp.blob(), name);
      } catch {
        /* 取回失败不打断动作结果展示 */
      }
    }
  }

  async function exportBackup(args) {
    const [scope, encrypted, ids] = args;
    const data = await post('/api/export_backup', {
      scope: scope || 'all',
      encrypted: encrypted !== false,
      credential_ids: ids || null,
    });
    if (!data || data.ok === false) return data;
    downloadText(JSON.stringify(data.document, null, 2), data.filename);
    return {
      ok: true,
      path: data.filename,
      count: data.count,
      encrypted: data.encrypted,
      scope: data.scope,
    };
  }

  // 导入文件在浏览器里选；补问原主密码时会带同一个文档重试，所以缓存在闭包里
  let pendingImport = null;

  function pickJsonFile() {
    return new Promise((resolve) => {
      const input = document.createElement('input');
      input.type = 'file';
      input.accept = '.json,application/json';
      input.style.display = 'none';
      input.addEventListener('change', async () => {
        const file = input.files && input.files[0];
        input.remove();
        if (!file) {
          resolve(null);
          return;
        }
        try {
          const text = await file.text();
          resolve({ name: file.name, doc: JSON.parse(text) });
        } catch {
          resolve({ name: file.name, error: 'JSON 解析失败，请确认文件是 Keyrealm 备份' });
        }
      });
      document.body.appendChild(input);
      input.click();
    });
  }

  async function importBackup(args) {
    const [password] = args;
    if (!pendingImport) {
      pendingImport = await pickJsonFile();
      if (!pendingImport) return { ok: false, message: '已取消' };
      if (pendingImport.error) {
        const message = pendingImport.error;
        pendingImport = null;
        return { ok: false, message };
      }
    }
    const data = await post('/api/import_backup', {
      document: pendingImport.doc,
      source: pendingImport.name,
      orig_password: password || null,
    });
    if (!(data && data.need_password)) pendingImport = null;
    return data;
  }

  async function rpc(method, args) {
    switch (method) {
      case 'open_external_url': {
        const url = String(args[0] || '');
        // 与后端 open_external_url 保持同一份白名单
        const allowed = ['https://github.com/YZBRH/keyrealm'];
        if (!allowed.includes(url)) return { ok: false, message: '不允许打开该链接' };
        window.open(url, '_blank', 'noopener');
        return { ok: true };
      }
      case 'open_log_file': {
        const name = encodeURIComponent(String(args[0] || ''));
        window.open(`/api/log_file?name=${name}&token=${encodeURIComponent(token)}`, '_blank');
        return { ok: true };
      }
      case 'minimize_to_tray':
        return { ok: true };
      case 'get_autostart':
        return { enabled: false, supported: false };
      case 'set_autostart':
        return { ok: false, message: 'Web 模式不支持开机自启' };
      case 'get_light_mode':
        return { enabled: false, supported: false };
      case 'set_light_mode':
        return { ok: true };
      case 'get_close_action':
        return { action: 'quit' };
      case 'set_close_action':
      case 'confirm_close_action':
        return { ok: true };
      case 'pick_save_path': {
        // 浏览器没有本机「另存为」对话框：由后端分配服务器临时路径，
        // 动作完成后自动取回并触发浏览器下载（见 downloadPending）。
        const wanted = String(args[0] || '');
        const data = await post('/api/call', { method: 'web_pick_save_path', args: [wanted] });
        if (data && data.ok) {
          pendingDownloads.set(data.path, data.name || wanted || 'download');
          return { ok: true, path: data.path };
        }
        return data || { ok: false, message: '无法分配临时文件' };
      }
      case 'run_credential_action': {
        const result = await post('/api/call', { method, args });
        // 非流式动作已执行完毕，插件此时才真正写完文件
        if (!(result && (result.ok === false || result.error))) await downloadPending();
        return result;
      }
      case 'stream_credential_action':
        // 流式动作要等 SSE 的 done 事件，见下方 onmessage
        return post('/api/call', { method, args });
      case 'export_backup':
        return exportBackup(args);
      case 'import_backup':
        return importBackup(args);
      default:
        return post('/api/call', { method, args });
    }
  }

  // 注意：`api` 会被 `Promise.resolve()` 等当成 thenable 探测（读 `.then`），
  // 若对任何属性名都返回函数，就会把 `then` 当成后端方法调用并报「未实现接口」。
  // 因此 then/catch/finally 与 Symbol 属性一律返回 undefined。
  window.pywebview = {
    api: new Proxy({}, {
      get(_target, method) {
        if (typeof method === 'symbol') return undefined;
        if (method === 'then' || method === 'catch' || method === 'finally') return undefined;
        return (...args) => rpc(String(method), args);
      },
    }),
  };

  // 后端 push_event → SSE → 浏览器 CustomEvent（与桌面端 pywebview 的事件同名同形）
  try {
    const source = new EventSource(`/api/events?token=${encodeURIComponent(token)}`);
    source.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data && data.event) {
          window.dispatchEvent(new CustomEvent(data.event, { detail: data.detail }));
          // 流式动作结束：取回插件写到服务器临时目录的下载文件
          if (data.event === 'vault:action_stream' && data.detail && data.detail.done) {
            downloadPending();
          }
        }
      } catch {
        /* 忽略坏帧 */
      }
    };
  } catch {
    /* EventSource 不可用时，功能仍可用，只是没有实时进度 */
  }

  window.dispatchEvent(new Event('pywebviewready'));
})();
