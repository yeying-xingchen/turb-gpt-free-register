/* Shared checkout UI. Credentials stay in the current form or on the server. */
(() => {
  'use strict';
  const TYPES = Object.freeze({
    extract: ['pix', 'upi', 'kakao_pay', 'ideal'],
    lumen: ['ideal', 'upi', 'pix', 'paypal', 'kakao_pay', 'momo', 'blik', 'twint', 'gcash', 'gopay'],
    upi_git5: ['upi'],
  });
  const PROVIDER_LABELS = Object.freeze({extract: 'Extract', lumen: 'Lumen Flow', upi_git5: 'UPI-GIT5'});
  const UPI_API_BASE = 'https://upi.agiapi.top';
  const STATUS = {queued: '提链排队', running: '提链中', awaiting_blik: '等待 BLIK 验证码', success: 'Checkout 成功', failed: '提链失败', stopped: '已停止', unknown: '状态未知，需核实', interrupted: '任务中断，需核实'};
  const BUSY = new Set(['queued', 'running', 'awaiting_blik']);
  const UNCERTAIN = new Set(['unknown', 'interrupted']);
  const accountLocks = new Set();
  let active = null;
  let sequence = 0;
  const text = value => value == null ? '' : String(value);
  const escape = value => text(value).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const enabled = value => value !== false && value !== 0;
  function element(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === 'text') node.textContent = text(value);
      else if (key === 'class') node.className = value;
      else if (key === 'value' || key === 'checked' || key === 'disabled' || key === 'hidden') node[key] = value;
      else node.setAttribute(key, text(value));
    }
    for (const child of [].concat(children)) if (child != null) node.append(child);
    return node;
  }
  function button(label, action, className = '') {
    const node = element('button', {type: 'button', text: label, class: className});
    if (action) node.addEventListener('click', action);
    return node;
  }
  function field(label, input, hint = '') {
    const id = input.id || `extract-field-${++sequence}`;
    input.id = id;
    const wrapper = element('div', {class: 'el-field'}, [element('label', {for: id, text: label}), input]);
    if (hint) {
      const hintId = `${id}-hint`;
      input.setAttribute('aria-describedby', hintId);
      wrapper.append(element('p', {id: hintId, class: 'el-hint', text: hint}));
    }
    return wrapper;
  }
  function check(label, checked = false) {
    const input = element('input', {type: 'checkbox', checked});
    return {input, wrap: element('label', {class: 'el-check'}, [input, element('span', {text: label})])};
  }
  function option(select, value, label, disabled = false) {
    select.append(element('option', {value: text(value), text: label, disabled}));
  }
  function safeUrl(value, image = false) {
    try {
      const url = new URL(text(value));
      if (['https:', 'http:'].includes(url.protocol) && !url.username && !url.password) return url.href;
      if (image && /^data:image\/(png|jpeg|webp);base64,[a-z\d+/=\s]+$/i.test(text(value))) return value;
    } catch (_) { /* Omit untrusted or non-HTTP links. */ }
    return '';
  }
  function getAccount(id) {
    if (typeof ACCOUNTS !== 'undefined') {
      const row = ACCOUNTS.find(r => Number(r.id) === Number(id));
      if (row) return row;
    }
    if (typeof ACCOUNT_SELECTED_ROWS !== 'undefined') return ACCOUNT_SELECTED_ROWS.get(Number(id));
    return null;
  }
  async function reloadAccounts() {
    if (typeof loadAccounts === 'function') await loadAccounts();
  }
  async function request(url, method = 'GET', body) {
    let response;
    try {
      response = await fetch(url, {method, credentials: 'same-origin', headers: {'Accept': 'application/json', ...(body === undefined ? {} : {'Content-Type': 'application/json'})}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
    } catch (_) {
      const error = new Error(method === 'GET' ? '网络请求失败，请检查连接后重新加载。' : '网络中断，操作结果尚未确认。请先刷新账号或配置核实，避免重复提交。');
      error.uncertain = method !== 'GET';
      throw error;
    }
    let data;
    try { data = await response.json(); } catch (_) {
      const error = new Error(`服务器返回了无法解析的响应（HTTP ${response.status}）。请刷新核实结果。`);
      error.uncertain = method !== 'GET';
      throw error;
    }
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      const error = new Error('服务器返回了无效的响应，请刷新核实结果。');
      error.uncertain = method !== 'GET';
      throw error;
    }
    if (!response.ok || data.ok === false) {
      const reason = data.error || data.message || `HTTP ${response.status}`;
      const error = new Error(typeof reason === 'string' ? reason : JSON.stringify(reason));
      error.payload = data;
      error.uncertain = method !== 'GET' && (response.status >= 500 || response.status === 408);
      throw error;
    }
    return data;
  }
  function modal(title) {
    if (active?.busy) { active.dialog.focus(); return null; }
    active?.dialog.close();
    const previous = document.activeElement;
    const titleId = `extract-title-${++sequence}`;
    const dialog = element('dialog', {class: 'el-dialog', 'aria-labelledby': titleId, tabindex: '-1'});
    const close = button('关闭', () => dialog.close(), 'el-close');
    close.setAttribute('aria-label', '关闭对话框');
    const body = element('div', {class: 'el-body'});
    const error = element('div', {class: 'el-error', role: 'alert', hidden: true, tabindex: '-1'});
    const status = element('div', {class: 'el-feedback', role: 'status', 'aria-live': 'polite', hidden: true});
    dialog.append(element('header', {class: 'el-header'}, [element('h2', {id: titleId, text: title}), close]), body, error, status);
    const state = {dialog, body, error, status, busy: false, live: () => active === state && dialog.open};
    state.fail = value => { if (!state.live()) return; error.textContent = text(value?.message || value); error.hidden = false; error.focus(); };
    state.message = value => { if (!state.live()) return; status.textContent = text(value); status.hidden = !value; };
    dialog.addEventListener('cancel', event => {
      if (state.busy && !state.allowBusyClose) { event.preventDefault(); state.message('正在等待服务器响应，请稍候再关闭。'); }
    });
    dialog.addEventListener('close', () => {
      dialog.querySelectorAll('input[type=password]').forEach(input => { input.value = ''; });
      if (active === state) active = null;
      dialog.remove();
      if (previous?.isConnected) previous.focus();
    });
    state.run = async fn => {
      if (state.busy || !state.live()) return;
      state.busy = true;
      error.hidden = true;
      status.hidden = true;
      dialog.setAttribute('aria-busy', 'true');
      const controls = Array.from(dialog.querySelectorAll('button, input, select, textarea, fieldset')).filter(control => !(state.allowBusyClose && control === close));
      const wasDisabled = controls.map(control => control.disabled);
      controls.forEach(control => { control.disabled = true; });
      try { await fn(); }
      catch (err) { state.fail(err); }
      finally {
        state.busy = false;
        dialog.removeAttribute('aria-busy');
        controls.forEach((control, index) => { if (control.isConnected) control.disabled = wasDisabled[index]; });
      }
    };
    document.body.append(dialog);
    active = state;
    dialog.showModal();
    return state;
  }
  function fillTypes(select, providerType, preferred) {
    select.replaceChildren();
    for (const type of TYPES[providerType] || []) option(select, type, type.toUpperCase());
    if ((TYPES[providerType] || []).includes(preferred)) select.value = preferred;
  }
  function masked(cdk) { return `••••${text(cdk.display_suffix || '').slice(-8)}`; }
  function redact(value) {
    if (Array.isArray(value)) return value.map(redact);
    if (!value || typeof value !== 'object') return value;
    return Object.fromEntries(Object.entries(value).map(([key, val]) => [key, /^(cdk|raw_cdk|access_?token|authorization|api_?key|secret|password)$/i.test(key) ? '[隐藏]' : redact(val)]));
  }
  function balance(container, result) {
    container.replaceChildren();
    const payload = result.result || result;
    const sources = [payload, payload.data, payload.balance, payload.cdk, payload.link_cdk, payload.data?.balance, payload.data?.cdk, payload.data?.link_cdk].filter(value => value && typeof value === 'object');
    const valueFor = key => sources.find(value => value[key] != null)?.[key];
    const valid = valueFor('valid');
    const quotas = [['剩余次数', 'remaining_uses'], ['可用次数', 'available_uses'], ['预留次数', 'reserved_uses']];
    const summary = quotas.filter(([, key]) => valueFor(key) != null).map(([label, key]) => `${label}：${text(valueFor(key))}`);
    if (summary.length) container.append(element('p', {text: summary.join(' · ')}));
    container.append(element('p', {text: valid === false ? 'CDK 验证未通过' : 'CDK 验证响应（剩余为 0 且有预留次数时仍可能有效）'}));
    container.append(element('pre', {class: 'el-json', text: JSON.stringify(redact(result), null, 2)}));
    container.hidden = false;
  }
  async function open(ids) {
    ids = [...new Set([].concat(ids || []).map(Number).filter(id => Number.isSafeInteger(id) && id > 0))];
    const state = modal('选择提链服务商');
    if (!state) return;
    if (!ids.length) { state.fail('请先选择账号。服务商和 CDK 可通过账号工具栏独立管理。'); return; }
    const blocked = ids.map(getAccount).filter(row => row && (UNCERTAIN.has(row.extract_link_status) || BUSY.has(row.extract_link_status)));
    if (blocked.length || ids.some(id => accountLocks.has(id))) { state.fail('所选账号包含执行中或待核实的任务。请先刷新任务状态；未知或中断任务不会自动重试。'); return; }
    const form = element('form');
    const fields = element('fieldset', {class: 'el-fields', disabled: true});
    const providerSelect = element('select', {required: '', 'aria-label': '提链服务商'});
    option(providerSelect, '', '请选择服务商（每次提交必须选择）');
    const typeSelect = element('select', {required: ''});
    const credentials = element('select', {required: ''});
    const raw = element('input', {type: 'password', autocomplete: 'new-password', spellcheck: 'false'});
    const memo = element('input', {type: 'text', maxlength: '200', autocomplete: 'off'});
    const save = check('提交前将此新 CDK 保存到当前服务商（可选）');
    const rawSection = element('div', {hidden: true}, [field('本次使用的 CDK', raw), save.wrap, field('保存备注（可选）', memo)]);
    const proxy = element('input', {type: 'url', autocomplete: 'off', placeholder: 'http://user:password@host:port'});
    const proxySection = field('代理 URL（可选）', proxy, '仅 Lumen 使用；留空采用服务商默认连接。');
    proxySection.hidden = true;
    const entryProxies = element('textarea', {rows: '4', autocomplete: 'off', spellcheck: 'false', placeholder: 'http://user:password@proxy.example:8080\nsocks5://proxy.example:1080'});
    const entryProxySection = field('UPI 入口代理（必填，每行一个 URL）', entryProxies, '请提供 UPI 服务端可连接的代理。不会自动使用本机或本地服务器代理池；代理数量无需与账号数量一致，所选账号将合并为一个批次。');
    entryProxySection.hidden = true;
    state.dialog.addEventListener('close', () => { proxy.value = ''; entryProxies.value = ''; });
    const providerInfo = element('p', {class: 'el-hint'});
    const validation = element('div', {class: 'el-validation', hidden: true, 'aria-live': 'polite'});
    const validate = button('验证 CDK / 查看余额与费用', () => state.run(async () => balance(validation, await request('/api/extract-link/cdk', 'POST', credentialBody()))));
    const submit = element('button', {type: 'submit', class: 'el-primary', text: `提交 ${ids.length} 个账号的提链任务`});
    fields.append(field('服务商（必选）', providerSelect), providerInfo, field('支付方式', typeSelect), field('CDK 来源（必选）', credentials), rawSection, proxySection, entryProxySection, validate, validation);
    form.append(fields, element('p', {class: 'el-notice', text: '支付金额固定为 0。Checkout 成功表示已生成结账结果，不代表已支付或已开通 Plus；请核实支付状态及套餐。服务商可能按任务消耗 CDK 次数。'}), submit);
    const manageButton = button('管理服务商 / CDK', () => manage());
    state.body.append(element('p', {class: 'el-hint', text: `已选择 ${ids.length} 个账号。仅支持符合试用条件的账号，服务端会检查资格。`}), manageButton, form);
    submit.disabled = true;
    let providers = [];
    function provider() {
      if (providerSelect.value === '') throw new Error('请选择服务商，系统不会自动选择默认服务商。');
      const selected = providers.find(p => text(p.id) === providerSelect.value);
      if (!selected || !enabled(selected.enabled)) throw new Error('服务商不可用，请重新选择。');
      return selected;
    }
    function credentialBody() {
      const selected = provider();
      const body = {provider_id: Number(selected.id)};
      if (credentials.value === 'raw') {
        if (!raw.value.trim()) { raw.focus(); throw new Error('请输入本次使用的 CDK。'); }
        body.cdk = raw.value.trim();
      } else if (credentials.value.startsWith('saved:')) {
        body.cdk_id = Number(credentials.value.slice(6));
      } else if (!(credentials.value === 'configured' && Number(selected.id) === 0 && selected.has_configured_cdk)) {
        throw new Error('请选择已保存的 CDK，或输入新 CDK。');
      }
      return body;
    }
    function selectProvider() {
      raw.value = ''; memo.value = ''; proxy.value = ''; entryProxies.value = ''; entryProxies.required = false; entryProxySection.hidden = true; save.input.checked = false; raw.required = false; rawSection.hidden = true; validation.hidden = true; credentials.replaceChildren(); option(credentials, '', '请选择 CDK 来源'); typeSelect.replaceChildren(); option(typeSelect, '', '请先选择服务商'); proxySection.hidden = true; providerInfo.textContent = '';
      if (providerSelect.value === '') return;
      const selected = provider();
      fillTypes(typeSelect, selected.provider_type, selected.default_link_type);
      providerInfo.textContent = `${selected.name} · ${PROVIDER_LABELS[selected.provider_type] || selected.provider_type}${Number(selected.id) === 0 ? ' · 环境配置（只读）' : ''}${selected.note ? ' · ' + selected.note : ''}`;
      for (const cdk of selected.cdks || []) if (enabled(cdk.enabled)) option(credentials, `saved:${cdk.id}`, `${masked(cdk)}${cdk.memo ? ' · ' + cdk.memo : ''}`);
      if (Number(selected.id) === 0 && selected.has_configured_cdk) option(credentials, 'configured', '使用服务器环境配置的 CDK');
      option(credentials, 'raw', '输入新 CDK（仅本次使用，保存需勾选）');
      save.wrap.hidden = Number(selected.id) === 0; memo.parentElement.hidden = Number(selected.id) === 0; proxySection.hidden = selected.provider_type !== 'lumen';
      entryProxies.required = selected.provider_type === 'upi_git5'; entryProxySection.hidden = !entryProxies.required;
    }
    providerSelect.addEventListener('change', selectProvider);
    credentials.addEventListener('change', () => { raw.value = ''; save.input.checked = false; memo.value = ''; validation.hidden = true; rawSection.hidden = credentials.value !== 'raw'; raw.required = credentials.value === 'raw'; if (raw.required) raw.focus(); });
    raw.addEventListener('input', () => { validation.hidden = true; });
    form.addEventListener('submit', event => {
      event.preventDefault();
      if (state.busy || !form.reportValidity()) return;
      state.run(async () => {
        if (ids.some(id => accountLocks.has(id))) throw new Error('任务正在提交，请勿重复操作。');
        let body = credentialBody();
        const selected = provider();
        body.link_type = typeSelect.value.toLowerCase();
        if (!(TYPES[selected.provider_type] || []).includes(body.link_type)) throw new Error('请选择支持的支付方式。');
        body.payment_amount = 0;
        if (selected.provider_type === 'lumen' && proxy.value.trim()) body.proxy_url = proxy.value.trim();
        if (selected.provider_type === 'upi_git5') {
          body.entry_proxies = entryProxies.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
          if (!body.entry_proxies.length) throw new Error('请填写 UPI 服务端可连接的入口代理，每行一个 URL。');
          for (const [index, value] of body.entry_proxies.entries()) {
            let valid = false;
            try { const url = new URL(value); valid = !!url.hostname && ['http:', 'https:', 'socks4:', 'socks4a:', 'socks5:', 'socks5h:'].includes(url.protocol) && !/\s/.test(value); } catch (_) { /* Report the line, without exposing proxy credentials. */ }
            if (!valid) throw new Error(`第 ${index + 1} 个入口代理必须是有效的 HTTP(S) 或 SOCKS URL。`);
          }
        }
        ids.forEach(id => accountLocks.add(id));
        try {
          if (body.cdk && save.input.checked && Number(selected.id) !== 0) {
            const saved = await request(`/api/extract-link/providers/${selected.id}/cdks`, 'POST', {cdk: body.cdk, memo: memo.value.trim(), enabled: true});
            const item = saved.item || saved.cdk || saved;
            const savedId = item.id ?? saved.cdk_id;
            if (!Number.isSafeInteger(Number(savedId)) || Number(savedId) <= 0) throw new Error('CDK 已保存，但服务器未返回 ID。请到管理窗口核实后再提交。');
            option(credentials, `saved:${savedId}`, `${masked(item)}${memo.value.trim() ? ' · ' + memo.value.trim() : ''}`); credentials.value = `saved:${savedId}`; body.cdk_id = Number(savedId); delete body.cdk; raw.value = ''; raw.required = false; save.input.checked = false; rawSection.hidden = true;
          }
          const single = ids.length === 1;
          const result = await request(single ? '/api/accounts/extract-link' : '/api/accounts/extract-link-bulk', 'POST', {...body, ...(single ? {account_id: ids[0]} : {account_ids: ids})});
          raw.value = ''; proxy.value = ''; entryProxies.value = ''; form.hidden = true;
          const skipped = Number(result.skipped_count || 0) + Number(result.busy_count || 0) + Number(result.failed_count || 0);
          state.message(result.message || (single ? '提链任务已提交。请在账号列表查看进度。' : `已入队 ${result.started_count || 0} 个；跳过 / 失败 ${skipped} 个。`));
          if (skipped || result.errors?.length || result.skipped?.length) state.body.append(element('pre', {class: 'el-json', text: JSON.stringify(redact(result), null, 2)}));
          await reloadAccounts();
        } catch (err) {
          if (err.uncertain) { form.hidden = true; raw.value = ''; proxy.value = ''; entryProxies.value = ''; state.body.append(button('刷新账号以核实结果', () => state.run(reloadAccounts))); }
          throw err;
        } finally { ids.forEach(id => accountLocks.delete(id)); }
      });
    });
    async function loadProviders() {
      try {
        const result = await request('/api/extract-link/providers');
        if (!state.live()) return;
        providers = result.items || [];
        providerSelect.replaceChildren(); option(providerSelect, '', '请选择服务商（每次提交必须选择）');
        for (const item of providers) if (enabled(item.enabled)) option(providerSelect, item.id, `${item.name} · ${PROVIDER_LABELS[item.provider_type] || item.provider_type}${item.is_default ? '（配置默认；仍需手动选择）' : ''}`);
        fields.disabled = false; submit.disabled = false; selectProvider(); providerSelect.focus();
        if (!providers.some(p => enabled(p.enabled))) state.fail('尚无启用的服务商，请先管理服务商并配置 CDK。');
      } catch (err) {
        if (!state.live()) return;
        state.fail(err); state.body.append(button('重新加载服务商', async event => { event.currentTarget.remove(); await loadProviders(); }));
      }
    }
    await loadProviders();
  }
  function newScanIdempotencyKey() {
    const value = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    return `scan-bulk-${value}`.slice(0, 128);
  }
  const PAYMENT_STATUS = {submitting: '提交处理中', submission_pending: '提交结果待确认', awaiting_worker: '等待处理人员', pending: '提交处理中', created: '已创建', queued: '排队中', claimed: '已领取', assigned: '已分配', running: '处理中', processing: '处理中', verifying: '核验中', in_progress: '处理中', succeeded: '服务商结算成功', success: '服务商结算成功', paid: '服务商报告已支付', completed: '服务商结算完成', failed: '失败', rejected: '已拒绝', canceled: '已取消', cancelled: '已取消', expired: '已过期', timeout: '已超时', not_activated: '未成功激活（已退次）', unknown: '结果待核实', interrupted: '结果待核实', not_submitted: '没有提交记录', no_task: '没有任务 ID'};
  const PAYMENT_ACTIVE = new Set(['submitting', 'submission_pending', 'awaiting_worker', 'pending', 'created', 'queued', 'claimed', 'assigned', 'running', 'processing', 'verifying', 'in_progress']);
  const PAYMENT_FAILED = new Set(['failed', 'rejected', 'canceled', 'cancelled', 'expired', 'timeout', 'not_activated']);
  const PAYMENT_SUCCESS = new Set(['succeeded', 'success', 'paid', 'completed']);
  const PAYMENT_RETRY = new Set(['v1', 'orderhub']);
  const PAYMENT_GROUP = {created: '新建任务', duplicated: '复用已有任务', pending: '提交处理中，尚未确认创建', items: '查询结果', failed: '处理失败', unknown: '结果待核实'};
  function paymentProvider(provider) { return provider === 'masi' ? 'Masi · masi.cc.cd' : provider === 'orderhub' ? 'UPI OrderHub · upi.xxsyun.xyz' : provider === 'seashore' ? 'seashore 发布者 API · seashore.lol' : provider === 'v1' ? 'Astra Scan Workbench · scan-qr.hixinghai.com' : '原提交服务商'; }
  function paymentCell(row) {
    const status = text(row.scan_request_status).toLowerCase();
    if (!status && !row.scan_request_task_id) return '';
    const klass = PAYMENT_SUCCESS.has(status) ? 'status-success' : PAYMENT_FAILED.has(status) ? 'status-failed' : PAYMENT_ACTIVE.has(status) ? 'status-running' : 'status-used';
    const parts = [`<span class="pill ${klass}">支付任务：${escape(PAYMENT_STATUS[status] || status || '待查询')}</span>`];
    if (row.scan_request_provider) parts.push(`<div class="el-row-detail">${escape(paymentProvider(row.scan_request_provider))}</div>`);
    if (row.scan_request_task_id) parts.push(`<div class="el-row-detail">任务 ID：${escape(row.scan_request_task_id)}</div>`);
    for (const value of new Set([row.scan_request_message, row.scan_request_error].filter(Boolean))) parts.push(`<div class="el-row-detail">${escape(value)}</div>`);
    if (row.scan_request_checked_at) parts.push(`<div class="el-row-detail">更新：${escape(row.scan_request_checked_at)}</div>`);
    return `<div class="extract-link-cell el-payment-cell">${parts.join('')}</div>`;
  }
  const ACTIVATION_STATUS = {queued: '排队中', checking: '检查试用资格', extracting: '生成 UPI 结账链接', paying: '支付处理中', verifying: '核验真实 Plus 套餐', succeeded: '已核验 Plus', failed: '开通失败', needs_attention: '结果待核实', interrupted: '任务中断，待核实'};
  const ACTIVATION_ACTIVE = new Set(['queued', 'checking', 'extracting', 'paying', 'verifying']);
  function activationTime(value) {
    const raw = text(value).trim();
    if (!raw) return '';
    const timestamp = Number(raw);
    if (Number.isFinite(timestamp) && timestamp <= 0) return '';
    const date = new Date(Number.isFinite(timestamp) ? timestamp * 1000 : raw);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
  }
  function activationCell(row) {
    const status = text(row.plus_activation_status).toLowerCase();
    if (!status) return '';
    const klass = status === 'succeeded' ? 'status-success' : status === 'failed' ? 'status-failed' : ACTIVATION_ACTIVE.has(status) ? 'status-running' : 'status-used';
    const parts = [`<span class="pill ${klass}">Plus 开通：${escape(ACTIVATION_STATUS[status] || '结果待核实')}</span>`];
    if (row.plus_activation_message) parts.push(`<div class="el-row-detail">${escape(row.plus_activation_message)}</div>`);
    const updatedAt = activationTime(row.plus_activation_updated_at);
    if (updatedAt) parts.push(`<div class="el-row-detail">更新：${escape(updatedAt)}</div>`);
    return `<div class="extract-link-cell el-activation-cell">${parts.join('')}</div>`;
  }
  // Candidate order is submitted once; the backend owns failure detection and fallback.
  function activationCandidates(state, form, label, heading, create, onChange) {
    const items = [];
    let locked = false;
    const section = element('section', {class: 'el-activation-config'});
    const list = element('div', {class: 'el-activation-candidates', 'aria-label': `${label}候选列表`});
    const count = element('span', {class: 'el-hint', role: 'status', 'aria-live': 'polite'});
    const add = button(`添加${label}候选`, () => append(true));
    section.append(element('h3', {text: heading}), list, element('div', {class: 'el-candidate-add'}, [add, count]));
    form.append(section);
    function sync(disabled = locked) {
      locked = disabled;
      add.disabled = locked || items.length >= 20;
      count.textContent = `${items.length} / 20 项 · 按从上到下顺序尝试`;
      items.forEach((item, index) => {
        item.title.textContent = `${label}候选 ${index + 1}${index === 0 ? ' · 优先尝试' : ' · 失败后备选'}`;
        item.remove.disabled = locked || items.length === 1;
        item.up.disabled = locked || index === 0;
        item.down.disabled = locked || index === items.length - 1;
        for (const [control, action] of [[item.remove, '删除'], [item.up, '上移'], [item.down, '下移']]) control.setAttribute('aria-label', `${action}${label}候选 ${index + 1}`);
        item.candidate.sync(locked);
      });
    }
    function changed() { sync(); onChange(); }
    function move(item, delta) {
      if (locked || state.busy || !state.live()) return;
      const from = items.indexOf(item), to = from + delta;
      if (from < 0 || to < 0 || to >= items.length) return;
      items.splice(from, 1); items.splice(to, 0, item);
      for (const entry of items) list.append(entry.node);
      changed();
    }
    function append(focus = false) {
      if (locked || state.busy || !state.live() || items.length >= 20) return;
      const title = element('legend');
      const candidate = create();
      const node = element('fieldset', {class: 'el-activation-candidate'}, [title]);
      const item = {candidate, node, title};
      item.remove = button('删除', () => {
        if (locked || state.busy || !state.live() || items.length <= 1) return;
        candidate.clear(); items.splice(items.indexOf(item), 1); node.remove(); changed(); add.focus();
      }, 'el-danger');
      item.up = button('上移', () => move(item, -1));
      item.down = button('下移', () => move(item, 1));
      node.append(element('div', {class: 'el-candidate-actions'}, [item.up, item.down, item.remove]), candidate.node);
      items.push(item); list.append(node); changed();
      if (focus) candidate.node.querySelector('select')?.focus();
    }
    append();
    return {sync, each: callback => items.forEach(item => callback(item.candidate)), clear: () => items.forEach(item => item.candidate.clear()), body: () => {
      const configs = items.map((item, index) => {
        try { return item.candidate.body(); }
        catch (error) { throw new Error(`${label}候选 ${index + 1}：${error.message}`); }
      });
      return configs.length === 1 ? configs[0] : configs;
    }, summary: () => items.map((item, index) => `${index + 1}. ${item.candidate.summary()}`).join(' → ')};
  }
  // One shared catalogue; each candidate keeps its own provider, CDK and proxies.
  function activationExtraction(state, form, onChange) {
    let providers = [];
    let ready = false;
    let loading = false;
    let locked = false;
    const candidates = activationCandidates(state, form, '提链', '1 · 提链配置', create, onChange);
    const retry = button('重新加载提链服务商', () => load()); retry.hidden = true; form.append(retry);
    function create() {
      const fields = element('fieldset', {class: 'el-fields el-activation-extraction', disabled: !ready});
      const provider = element('select', {required: '', 'aria-label': '提链服务商'});
      const credentials = element('select', {required: '', 'aria-label': '提链 CDK 来源'});
      const raw = element('input', {type: 'password', autocomplete: 'new-password', spellcheck: 'false', 'aria-label': '本次提链 CDK'});
      const rawField = field('本次提链 CDK', raw, '临时 CDK 仅用于本候选，提交、删除或关闭后清空。');
      const proxy = element('input', {type: 'url', autocomplete: 'off', 'aria-label': '提链代理 URL'});
      const proxyField = field('Lumen 代理 URL（可选）', proxy, '留空采用服务商默认连接。');
      const entries = element('textarea', {rows: '3', autocomplete: 'off', spellcheck: 'false', 'aria-label': 'UPI 入口代理'});
      const entryField = field('UPI 入口代理（必填，每行一个 URL）', entries, '填写 UPI 服务端可连接的 HTTP(S) 或 SOCKS 代理；账号共用本候选的代理。');
      const info = element('p', {class: 'el-hint'});
      option(credentials, '', '请先选择服务商');
      rawField.hidden = proxyField.hidden = entryField.hidden = true;
      fields.append(field('提链服务商', provider), field('提链 CDK 来源', credentials), info, rawField, proxyField, entryField);
      const selected = () => providers.find(item => text(item.id) === provider.value && enabled(item.enabled));
      function clear() { raw.value = ''; proxy.value = ''; entries.value = ''; }
      function chooseProvider() {
        clear(); credentials.replaceChildren(); option(credentials, '', '请选择提链 CDK'); credentials.value = '';
        raw.required = false; rawField.hidden = true;
        const item = selected();
        info.textContent = item ? `${item.name} · ${PROVIDER_LABELS[item.provider_type] || item.provider_type} · UPI / 0 元 Checkout` : '';
        for (const cdk of item?.cdks || []) if (enabled(cdk.enabled)) option(credentials, `saved:${cdk.id}`, `${masked(cdk)}${cdk.memo ? ' · ' + cdk.memo : ''}`);
        if (item && Number(item.id) === 0 && item.has_configured_cdk) option(credentials, 'configured', '使用服务器环境配置的 CDK');
        if (item) option(credentials, 'raw', '输入本次提链 CDK');
        proxyField.hidden = item?.provider_type !== 'lumen';
        entries.required = item?.provider_type === 'upi_git5'; entryField.hidden = !entries.required;
        onChange();
      }
      function fill() {
        provider.replaceChildren(); option(provider, '', '请选择提链服务商'); provider.value = '';
        for (const item of providers) if (enabled(item.enabled)) option(provider, item.id, `${item.name} · ${PROVIDER_LABELS[item.provider_type] || item.provider_type}`);
        chooseProvider();
      }
      provider.addEventListener('change', chooseProvider);
      credentials.addEventListener('change', () => { raw.value = ''; raw.required = credentials.value === 'raw'; rawField.hidden = !raw.required; onChange(); if (raw.required) raw.focus(); });
      raw.addEventListener('input', onChange);
      fill();
      return {node: fields, clear, fill, sync: disabled => { fields.disabled = !ready || disabled; }, body: () => {
        const item = selected();
        if (!ready || !item) throw new Error('请选择启用的提链服务商。');
        const result = {provider_id: Number(item.id)};
        if (credentials.value === 'raw') {
          if (!raw.value.trim()) throw new Error('请输入本次提链 CDK。');
          result.cdk = raw.value.trim();
        } else if (credentials.value.startsWith('saved:')) {
          const id = Number(credentials.value.slice(6));
          if (!(item.cdks || []).some(cdk => Number(cdk.id) === id && enabled(cdk.enabled))) throw new Error('请选择当前服务商可用的提链 CDK。');
          result.cdk_id = id;
        } else if (!(credentials.value === 'configured' && Number(item.id) === 0 && item.has_configured_cdk)) throw new Error('请选择提链 CDK 来源。');
        if (item.provider_type === 'lumen' && proxy.value.trim()) result.proxy_url = proxy.value.trim();
        if (item.provider_type === 'upi_git5') {
          result.entry_proxies = entries.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
          if (!result.entry_proxies.length) throw new Error('请填写 UPI 服务端可连接的入口代理，每行一个 URL。');
          for (const [index, value] of result.entry_proxies.entries()) {
            let valid = false;
            try { const url = new URL(value); valid = !!url.hostname && ['http:', 'https:', 'socks4:', 'socks4a:', 'socks5:', 'socks5h:'].includes(url.protocol) && !/\s/.test(value); } catch (_) { /* Never echo proxy credentials. */ }
            if (!valid) throw new Error(`第 ${index + 1} 个入口代理必须是有效的 HTTP(S) 或 SOCKS URL。`);
          }
        }
        return result;
      }, summary: () => {
        const item = selected();
        const cdk = (item?.cdks || []).find(value => `saved:${value.id}` === credentials.value);
        return `${item?.name || '未选择'} · 提链 CDK：${cdk ? masked(cdk) : credentials.value === 'configured' ? '服务器配置' : credentials.value === 'raw' ? (raw.value.trim() ? '已输入（隐藏）' : '待输入') : '未选择'}`;
      }};
    }
    async function load() {
      if (!state.live() || state.busy || loading || locked) return;
      loading = true; retry.disabled = true;
      try {
        const result = await request('/api/extract-link/providers');
        if (!state.live()) return;
        providers = result.items || [];
        ready = providers.some(item => enabled(item.enabled)); retry.hidden = ready;
        candidates.each(candidate => candidate.fill());
        if (!ready) state.fail('尚无启用的提链服务商，请在账号工具栏管理服务商 / CDK。');
      } catch (error) { if (state.live()) { state.fail(error); retry.hidden = false; } }
      finally { loading = false; onChange(); }
    }
    return {...candidates, load, sync: disabled => { locked = disabled; candidates.sync(disabled); retry.disabled = disabled || loading; }, get ready() { return ready; }};
  }
  // 已保存的支付 CDK：只在用户明确选择「使用已保存」时才读取，避免打开窗口就发请求。
  let paymentCatalogCache = null;
  async function loadPaymentCatalog(force = false) {
    if (paymentCatalogCache && !force) return paymentCatalogCache;
    const data = await request('/api/payment-providers');
    paymentCatalogCache = Array.isArray(data.items) ? data.items : [];
    return paymentCatalogCache;
  }
  function paymentCredentialPicker(providerValue, usesSession, onChange) {
    const source = element('select', {'aria-label': '支付凭据来源'});
    option(source, 'raw', '输入本次临时凭据'); option(source, 'saved', '使用已保存的支付 CDK');
    const savedSelect = element('select', {'aria-label': '已保存的支付 CDK'});
    const rawInput = element('input', {type: 'password', autocomplete: 'new-password', spellcheck: 'false'});
    const hint = element('p', {class: 'el-hint', role: 'status'});
    const sourceField = field('支付凭据来源', source);
    const savedField = field('已保存的支付 CDK', savedSelect, '在「服务与凭据 · 支付平台」保存后即可在此直接选用。');
    const rawField = field('本次支付 CDK', rawInput, '仅在此窗口内存中保留；关闭后清除。');
    const node = element('div', {class: 'el-payment-credential'}, [sourceField, savedField, rawField, hint]);
    let catalog = null, loading = false, locked = false;
    const savedItems = () => {
      const entry = (catalog || []).find(item => item.provider_type === providerValue());
      return ((entry && entry.cdks) || []).filter(item => enabled(item.enabled));
    };
    function fill() {
      savedSelect.replaceChildren();
      const items = savedItems();
      if (!items.length) option(savedSelect, '', loading ? '正在读取已保存的 CDK…' : '该平台还没有已保存的 CDK', true);
      for (const item of items) option(savedSelect, item.id, `${text(item.masked || item.display_suffix)}${item.memo ? ' · ' + text(item.memo) : ''}`);
      if (items.length) savedSelect.value = text(items[0].id);
    }
    async function ensureCatalog(force = false) {
      if (catalog && !force) return;
      loading = true; fill(); sync(locked);
      try { catalog = await loadPaymentCatalog(force); }
      catch (error) { catalog = null; hint.textContent = text(error?.message || error); }
      finally { loading = false; fill(); sync(locked); }
    }
    function sync(disabled) {
      locked = disabled === undefined ? locked : disabled;
      const session = usesSession();
      sourceField.hidden = session;
      savedField.hidden = session || source.value !== 'saved';
      rawField.hidden = session || source.value !== 'raw';
      source.disabled = rawInput.disabled = savedSelect.disabled = locked;
      // 必填只跟随当前可见的输入，避免隐藏的 required 让浏览器拒绝提交。
      rawInput.required = !session && source.value === 'raw';
      savedSelect.required = !session && source.value === 'saved';
      if (session) { hint.textContent = ''; return; }
      if (source.value !== 'saved') { hint.textContent = ''; return; }
      hint.textContent = loading ? '正在读取已保存的 CDK…'
        : savedItems().length ? '' : '该平台还没有已保存的 CDK，请先在「服务与凭据 · 支付平台」保存，或改回输入临时凭据。';
    }
    function reset() {
      source.value = 'raw'; rawInput.value = ''; savedSelect.replaceChildren();
      catalog = null; loading = false; hint.textContent = ''; fill(); sync(locked);
    }
    function body() {
      if (usesSession()) return {};
      if (source.value === 'saved') {
        const value = Number(savedSelect.value);
        if (!value) throw new Error('请选择已保存的支付 CDK，或改回输入本次临时凭据。');
        return {cdk_id: value};
      }
      if (!rawInput.value.trim()) throw new Error('请输入本次使用的支付 CDK / API Key。');
      return {cdk: rawInput.value.trim()};
    }
    function summary() {
      if (usesSession()) return 'OrderHub 已登录';
      if (source.value !== 'saved') return '支付 CDK / API Key：' + (rawInput.value.trim() ? '已输入（隐藏）' : '待输入');
      const item = savedItems().find(entry => Number(entry.id) === Number(savedSelect.value));
      return item ? `已保存 CDK ${text(item.masked || item.display_suffix)}` : '待选择已保存的 CDK';
    }
    source.addEventListener('change', () => { if (source.value === 'saved') ensureCatalog(); sync(locked); onChange(); });
    rawInput.addEventListener('input', onChange);
    savedSelect.addEventListener('change', onChange);
    fill();
    return {node, sync, reset, body, summary, focus: () => (source.value === 'saved' ? savedSelect : rawInput).focus(), get raw() { return rawInput; }, get source() { return source; }};
  }
  function activationPayment(state, form, onChange, sessionRequest) {
    // The API exposes one server-side OrderHub session, shared by session candidates.
    let sessionReady = false;
    let sessionMessage = '请登录，或检查服务器保存的登录状态。';
    const candidates = activationCandidates(state, form, '支付', '2 · 支付配置', create, onChange);
    function create() {
      const fields = element('fieldset', {class: 'el-fields el-activation-payment'});
      const provider = element('select', {'aria-label': '支付服务商'});
      option(provider, 'v1', 'Astra Scan Workbench · scan-qr.hixinghai.com');
      option(provider, 'masi', 'Masi · masi.cc.cd');
      option(provider, 'orderhub', 'UPI OrderHub · upi.xxsyun.xyz');
      option(provider, 'seashore', 'seashore 发布者 API · seashore.lol');
      provider.value = 'v1';
      const authentication = element('select', {'aria-label': '支付验证方式'});
      option(authentication, 'key', '支付 CDK / OrderHub API Key'); option(authentication, 'session', 'OrderHub 账号登录'); authentication.value = 'key';
      const authField = field('验证方式', authentication);
      const usesSession = () => provider.value === 'orderhub' && authentication.value === 'session';
      const picker = paymentCredentialPicker(() => provider.value, usesSession, onChange);
      const info = element('p', {class: 'el-hint'});
      const username = element('input', {type: 'text', inputmode: 'numeric', autocomplete: 'off', 'aria-label': 'OrderHub 数字账号'});
      const password = element('input', {type: 'password', autocomplete: 'new-password', 'aria-label': 'OrderHub 登录密码'});
      const sessionInfo = element('p', {class: 'el-hint', role: 'status'});
      const sessionFields = element('div', {hidden: true}, [field('OrderHub 数字账号', username), field('登录密码', password, '密码仅用于登录，响应后清空。所有 session 候选共用服务器当前 OrderHub 登录会话。'), element('div', {class: 'el-actions'}, [button('登录 OrderHub', () => sessionAction('login')), button('检查登录状态', () => sessionAction('session')), button('退出 OrderHub', () => sessionAction('logout'))]), sessionInfo]);
      const accountCenter = element('a', {href: 'https://upi.xxsyun.xyz', target: '_blank', rel: 'noopener noreferrer', text: '打开平台账号中心：注册、兑换 CDK、生成 API Key'});
      fields.append(field('支付平台', provider), authField, info, accountCenter, sessionFields, picker.node);
      function clear() { picker.reset(); username.value = ''; password.value = ''; }
      function sync(disabled) {
        fields.disabled = disabled;
        authField.hidden = accountCenter.hidden = provider.value !== 'orderhub';
        sessionFields.hidden = !usesSession();
        picker.sync(disabled);
        sessionInfo.textContent = sessionMessage;
        info.textContent = provider.value === 'orderhub' ? 'OrderHub：API Key 或数字账号登录；后台读取所选账号 AT。' : provider.value === 'masi' ? 'Masi：支付 CDK；后台读取所选账号 AT。' : provider.value === 'seashore' ? 'seashore 发布者 API：CDK 直接作为 Bearer 凭据；后台提交所选账号邮箱、完整 AT 与零元 UPI 链接。' : 'Astra Scan Workbench：支付 CDK；后台提交 UPI 链接和邮箱。';
      }
      provider.addEventListener('change', () => { clear(); authentication.value = 'key'; onChange(); });
      authentication.addEventListener('change', () => { clear(); onChange(); if (usesSession()) sessionAction('session'); });
      async function sessionAction(action) {
        if (state.busy || !state.live() || fields.disabled || !usesSession()) return;
        if (action === 'login' && (!/^[0-9]+$/.test(username.value.trim()) || !password.value)) { state.fail('请输入 OrderHub 数字账号和登录密码。'); return; }
        sessionReady = false;
        sessionMessage = '正在核实 OrderHub 登录状态…';
        await state.run(async () => {
          onChange();
          try {
            const {data} = await sessionRequest(`/api/payments/orderhub/${action}`, action === 'session' ? undefined : action === 'login' ? {username: username.value.trim(), password: password.value} : {}, action === 'session' ? 'GET' : 'POST');
            if (!state.live()) return;
            sessionReady = action === 'login' || (action === 'session' && data.logged_in === true);
            sessionMessage = sessionReady ? 'OrderHub 已登录（服务器当前会话）' : 'OrderHub 尚未登录，请输入账号和密码。';
          } catch (_) {
            sessionMessage = 'OrderHub 登录状态待核实，请重新检查登录状态。';
            state.fail(sessionMessage);
          } finally { password.value = ''; }
        });
        onChange();
      }
      return {node: fields, clear, sync, body: () => {
        if (!['v1', 'masi', 'orderhub', 'seashore'].includes(provider.value)) throw new Error('请选择支持的支付平台。');
        if (!['key', 'session'].includes(authentication.value) || (authentication.value === 'session' && provider.value !== 'orderhub')) throw new Error('仅 OrderHub 支持 session 登录验证。');
        if (usesSession() && !sessionReady) throw new Error('请先登录 OrderHub 或检查登录状态。');
        return {provider: provider.value, auth_mode: authentication.value, ...picker.body()};
      }, summary: () => `${paymentProvider(provider.value)} · ${usesSession() ? (sessionReady ? 'OrderHub 已登录' : 'OrderHub 待登录') : picker.summary()}`};
    }
    return {...candidates, clear: () => { candidates.clear(); sessionReady = false; }};
  }
  function activationSuccessGroup(state, form, onChange) {
    const defaultGroup = '默认分组';
    let groups = new Set([defaultGroup]);
    let loading = false;
    let locked = false;
    const select = element('select', {'aria-label': '成功后转入分组'});
    const info = element('p', {class: 'el-hint', role: 'status', 'aria-live': 'polite'});
    const retry = button('重新加载分组', () => load());
    const wrapper = field('成功后转入分组', select, '仅服务端核验账号真实 Plus 套餐成功后才转入所选分组；失败或待核实的账号保留原组。');
    wrapper.append(info, retry); form.append(wrapper);
    function fill(value = '') {
      select.replaceChildren(); option(select, '', '不自动转移');
      for (const name of groups) option(select, name, name);
      select.value = value;
    }
    fill();
    select.addEventListener('change', onChange);
    async function load() {
      if (!state.live() || state.busy || loading || locked) return;
      loading = true;
      const cleared = !!select.value && select.value !== defaultGroup;
      const value = select.value === defaultGroup ? defaultGroup : '';
      groups = new Set([defaultGroup]); fill(value);
      const resetNotice = cleared ? '原分组选择已清除，请重新选择目标分组。' : '';
      info.textContent = `正在加载分组…${resetNotice}`;
      onChange();
      try {
        const data = await request('/api/account-groups');
        if (!state.live()) return;
        if (!Array.isArray(data?.groups) || data.groups.some(item => typeof item?.group_name !== 'string' || !item.group_name.trim())) throw new Error('服务器返回的分组列表格式无效。');
        groups = new Set([defaultGroup, ...data.groups.map(item => item.group_name)]);
        fill(groups.has(select.value) ? select.value : '');
        info.textContent = `分组已加载。${resetNotice}`;
      } catch (error) {
        if (state.live()) info.textContent = `分组加载失败：${error.message} ${resetNotice}可重新加载，或选择“不自动转移” / “默认分组”继续提交。`;
      } finally { loading = false; onChange(); }
    }
    return {load, sync: disabled => { locked = disabled; select.disabled = disabled; retry.disabled = disabled || loading; }, value: () => {
      if (select.value !== '' && !groups.has(select.value)) throw new Error('目标分组不可用，请重新加载分组并选择。');
      return select.value;
    }, summary: () => select.value || '不自动转移'};
  }
  function paymentDialog(ids, queryOnly, activation = false) {
    ids = [...new Set([].concat(ids || []).map(Number).filter(id => Number.isSafeInteger(id) && id > 0))];
    const state = modal(activation ? '开通 Plus' : queryOnly ? '查询 UPI 支付任务' : '提交 UPI 支付任务');
    if (!state) return;
    if (!ids.length) { state.fail('请先选择账号。'); return; }
    state.allowBusyClose = true;
    // One key for the lifetime of this dialog, including every explicit retry.
    const batchKey = queryOnly || activation ? '' : newScanIdempotencyKey();
    const attempted = new Set();
    const results = new Map();
    let credential = null;
    let credentialSecret = '';
    let activationSecrets = [];
    let extraction = null;
    let activationPayments = null;
    let successGroup = null;
    let sessionReady = false;
    let selectedProvider = 'v1';
    let pollTimer = null;
    let controller = null;
    let pollStopped = true;
    let pollStartedAt = null;
    const pollExpired = () => pollStartedAt !== null && Date.now() - pollStartedAt >= 10 * 60 * 1000;
    const form = element('form');
    const provider = element('select', {'aria-label': '支付服务商'});
    option(provider, 'v1', 'Astra Scan Workbench（默认）· scan-qr.hixinghai.com/api/v1');
    option(provider, 'masi', 'Masi · https://masi.cc.cd');
    option(provider, 'orderhub', 'UPI OrderHub · https://upi.xxsyun.xyz/api/v1');
    option(provider, 'seashore', 'seashore 发布者 API · https://seashore.lol/api/publisher');
    provider.value = 'v1';
    const providerInfo = element('p', {class: 'el-notice'});
    const accountCenter = element('a', {href: 'https://upi.xxsyun.xyz', target: '_blank', rel: 'noopener noreferrer', hidden: true, text: '打开平台账号中心：注册、兑换 CDK、生成 API Key'});
    const authentication = element('select', {'aria-label': '支付验证方式'});
    option(authentication, 'key', '支付 CDK / OrderHub API Key');
    option(authentication, 'session', 'OrderHub 账号登录');
    authentication.value = 'key';
    const authField = field('验证方式', authentication);
    const usesSession = () => (queryOnly || provider.value === 'orderhub') && authentication.value === 'session';
    const picker = paymentCredentialPicker(() => provider.value, usesSession, () => summarize());
    const cdkField = picker.node;
    const credentialInfo = element('p', {class: 'el-hint', hidden: true, text: '凭据已输入，仅在当前窗口内存中用于提交和查询。关闭后清除。'});
    const sessionUsername = element('input', {type: 'text', inputmode: 'numeric', autocomplete: 'off', 'aria-label': 'OrderHub 数字账号'});
    const sessionPassword = element('input', {type: 'password', autocomplete: 'new-password', 'aria-label': 'OrderHub 登录密码'});
    const sessionInfo = element('p', {class: 'el-hint', role: 'status', text: '请登录，或检查服务器保存的登录状态。'});
    const login = button('登录 OrderHub', () => sessionAction('login'));
    const sessionCheck = button('检查登录状态', () => sessionAction('session'));
    const logout = button('退出 OrderHub', () => sessionAction('logout'));
    const sessionFields = element('div', {hidden: true}, [field('OrderHub 数字账号', sessionUsername), field('登录密码', sessionPassword, '密码仅用于本次登录，响应后清空；登录会话由服务器保存。'), element('div', {class: 'el-actions'}, [login, sessionCheck, logout]), sessionInfo]);
    const submit = element('button', {type: 'submit', class: 'el-primary', text: activation ? '开始开通 Plus' : queryOnly ? '查询所选账号' : '确认逐个提交支付'});
    const query = button('查询所选账号', () => execute('query', ids));
    query.hidden = queryOnly || activation;
    const retry = button('使用原请求安全重试', () => execute('submit', retryIds()));
    retry.hidden = true;
    const refresh = button('刷新账号列表', () => activation ? state.run(reloadAccounts).finally(syncControls) : execute('refresh', []));
    const actions = element('div', {class: 'el-actions'}, activation ? [submit] : [submit, query, retry, refresh]);
    const summary = element('p', {class: 'el-feedback', role: 'status', 'aria-live': 'polite'});
    const pollInfo = element('p', {class: 'el-hint', role: 'status', text: '提交或查询后，处理中任务每 5 秒自动查询一次；失败或结果待核实时停止自动查询。'});
    const list = element('div', {class: 'el-provider-list', 'aria-label': '所选账号支付结果'});
    state.body.append(element('p', {class: activation ? 'el-activation-count' : 'el-hint', text: activation ? `已选择 ${ids.length} 个账号 · UPI · 0 元 Checkout` : `已选择 ${ids.length} 个账号，包含跨页选择。每次请求只处理一个账号，由服务器检查 UPI 链接和提交记录。`}), form, summary, pollInfo, list);
    if (activation) {
      state.dialog.className += ' el-activation-dialog';
      list.setAttribute('aria-label', '所选账号 Plus 开通结果');
      pollInfo.textContent = '一次提交全部所选账号，后台自动检查资格、提链、支付并核验 Plus。账号列表每 10 秒更新进度。';
      form.append(element('p', {class: 'el-notice el-activation-fallback', text: '提链和支付分别按各自列表从上到下尝试，每组 1–20 项。仅在明确失败时自动切换下一候选；结果未知或超时继续查询、核实原任务，不切换支付配置，避免重复支付。'}));
      extraction = activationExtraction(state, form, () => { syncControls(); summarize(); });
      activationPayments = activationPayment(state, form, () => { syncControls(); summarize(); }, paymentRequest);
      successGroup = activationSuccessGroup(state, form, () => { syncControls(); summarize(); });
    } else {
      if (!queryOnly) form.append(field('支付服务商', provider));
      form.append(providerInfo, accountCenter, authField, sessionFields, cdkField, credentialInfo);
    }
    form.append(actions);
    state.body.append(element('p', {class: 'el-notice', text: activation ? '仅使用 UPI 零元 Checkout；提链与支付平台可能消耗 CDK 额度。关闭窗口后后台继续执行，本地凭据清空。仅服务端核验账号真实 Plus 套餐后显示成功，其他结果以待核实状态展示。' : '关闭窗口或等待超时只结束本地等待，不会取消服务器上的支付任务。支付结果以服务商返回的任务状态为准。'}));
    if (activation) state.body.append(refresh);
    const clean = value => {
      let valueText = text(value);
      for (const secret of [credentialSecret, sessionPassword.value, ...activationSecrets]) if (secret) valueText = valueText.split(secret).join('[已隐藏]');
      return valueText;
    };
    function providerDescription() {
      providerInfo.textContent = queryOnly ? '查询使用服务器保存的原提交服务商，无需填写任务 ID。可选择已保存的 CDK / API Key，或使用 OrderHub 登录会话。' : provider.value === 'orderhub' ? 'UPI OrderHub 由后端自动读取所选账号的完整 AT，无需粘贴。可使用 ohk_ 开头的 API Key 或数字账号登录。结果不确定时先查询；允许重试时沿用原请求。' : provider.value === 'masi' ? 'Masi 会由后端自动读取所选账号的完整 AT 并发送给 masi.cc.cd，无需粘贴 AT。结果不确定时仅查询，不重试创建。' : provider.value === 'seashore' ? 'seashore 发布者 API 由后端读取所选账号的邮箱与完整 AT，提交零元 UPI 链接。该平台没有幂等键，结果不确定时只查询、不重提，避免重复扣次。' : 'Astra Scan Workbench 使用 UPI 链接和邮箱，不发送账号 AT。结果不确定时，只能使用原请求安全重试。';
      authField.hidden = !queryOnly && provider.value !== 'orderhub';
      accountCenter.hidden = !queryOnly && provider.value !== 'orderhub';
      sessionFields.hidden = !usesSession();
      picker.sync(state.busy);
      if (activation) {
        providerInfo.textContent = provider.value === 'orderhub' ? 'UPI OrderHub：使用 API Key 或数字账号登录；由后台读取所选账号 AT 并完成开通任务。' : provider.value === 'masi' ? 'Masi：使用支付 CDK；由后台读取所选账号 AT 并完成开通任务。' : provider.value === 'seashore' ? 'seashore 发布者 API：使用支付 CDK；由后台读取所选账号邮箱与完整 AT 并完成开通任务。' : 'Astra Scan Workbench：使用支付 CDK；由后台提交 UPI 链接和邮箱。';
      }
      syncControls();
      if (activation) summarize();
    }
    provider.addEventListener('change', () => {
      if (activation) { picker.reset(); credential = null; credentialSecret = ''; sessionPassword.value = ''; authentication.value = 'key'; }
      providerDescription();
    });
    authentication.addEventListener('change', () => {
      stopPolling();
      if (activation) { picker.reset(); credential = null; credentialSecret = ''; sessionPassword.value = ''; }
      providerDescription();
      if (usesSession()) sessionAction('session');
    });
    async function sessionAction(action) {
      if (state.busy || !state.live()) return;
      if (action === 'login' && (!/^[0-9]+$/.test(sessionUsername.value.trim()) || !sessionPassword.value)) {
        state.fail('请输入 OrderHub 数字账号和登录密码。');
        return;
      }
      stopPolling();
      sessionReady = false;
      await state.run(async () => {
        try {
          const body = action === 'session' ? undefined : action === 'login' ? {username: sessionUsername.value.trim(), password: sessionPassword.value} : {};
          const {data} = await paymentRequest(`/api/payments/orderhub/${action}`, body, action === 'session' ? 'GET' : 'POST');
          if (!state.live()) return;
          sessionReady = action === 'login' || (action === 'session' && data.logged_in === true);
          sessionInfo.textContent = sessionReady ? `OrderHub 已登录：${clean(data.user?.username || data.user?.id || '当前用户')}` : 'OrderHub 尚未登录，请输入账号和密码。';
        } finally { sessionPassword.value = ''; }
      });
      syncControls();
      if (activation) summarize();
    }
    providerDescription();
    for (const id of ids) {
      const account = getAccount(id);
      const heading = element('strong', {text: `#${id}${account?.email ? ' · ' + account.email : ''}`});
      const detail = element('p', {text: queryOnly ? '等待查询' : '等待提交'});
      const card = element('article', {class: 'el-provider'}, [heading, detail]);
      list.append(card);
      results.set(id, {id, heading, detail, category: 'waiting', status: '', task_id: '', provider: account?.scan_request_provider || '', retryAllowed: false});
    }
    function summarize() {
      if (activation) {
        if (!attempted.size) summary.textContent = `已选择 ${ids.length} 个 · 提链顺序：${extraction?.summary() || '加载中'} · 支付顺序：${activationPayments?.summary() || '加载中'} · 成功后转入分组：${successGroup?.summary() || '不自动转移'}`;
        return;
      }
      const counts = {};
      for (const row of results.values()) counts[row.category] = (counts[row.category] || 0) + 1;
      const labels = {...PAYMENT_GROUP, waiting: queryOnly ? '未查询' : '未提交', working: '请求中'};
      summary.textContent = `已选择 ${ids.length} 个 · ` + Object.entries(counts).map(([key, count]) => `${labels[key] || key} ${count}`).join(' · ');
    }
    function retryIds() { return ids.filter(id => attempted.has(id) && results.get(id).retryAllowed && PAYMENT_RETRY.has(results.get(id).provider)); }
    function syncControls() {
      if (!state.live()) return;
      provider.disabled = state.busy || attempted.size > 0;
      extraction?.sync(state.busy || attempted.size > 0);
      activationPayments?.sync(state.busy || attempted.size > 0);
      successGroup?.sync(state.busy || attempted.size > 0);
      authentication.disabled = state.busy || attempted.size > 0 || !!credential;
      sessionCheck.disabled = state.busy;
      for (const control of [login, logout, sessionUsername]) control.disabled = state.busy || attempted.size > 0;
      submit.disabled = state.busy || (activation && !extraction?.ready) || (!queryOnly && ids.every(id => attempted.has(id)));
      submit.textContent = activation ? '开始开通 Plus' : queryOnly ? '查询所选账号' : attempted.size ? '继续提交未处理账号' : '确认逐个提交支付';
      query.disabled = state.busy;
      retry.hidden = !retryIds().length;
      retry.disabled = state.busy;
      refresh.disabled = state.busy;
      picker.sync(state.busy);
      cdkField.hidden = !!credential || usesSession();
      credentialInfo.hidden = !credential || usesSession();
    }
    function showResult(id, item, category, persisted = true) {
      const row = results.get(id);
      const task = item.task && typeof item.task === 'object' ? item.task : {};
      row.category = category;
      row.status = text(item.status || task.status || (category === 'pending' ? 'pending' : category === 'failed' ? 'failed' : category === 'unknown' ? 'unknown' : 'unknown')).toLowerCase();
      row.task_id = text(item.task_id || task.id || task.task_id || row.task_id);
      row.provider = item.provider || row.provider || (!queryOnly ? selectedProvider : '');
      row.retryAllowed = !queryOnly && attempted.has(id) && PAYMENT_RETRY.has(row.provider) && !row.task_id && (category === 'unknown' || category === 'failed') && (item.can_retry_same_key === true || (item.can_retry_same_key == null && row.retryAllowed));
      if (item.email) row.heading.textContent = `#${id} · ${clean(item.email)}`;
      const parts = [PAYMENT_GROUP[category], paymentProvider(row.provider), PAYMENT_STATUS[row.status] || row.status];
      if (row.task_id) parts.push(`任务 ID：${clean(row.task_id)}`);
      for (const message of new Set([item.message, item.error, task.message, task.error].filter(value => typeof value === 'string' && value))) parts.push(clean(message));
      if (item.code || task.failureCode) parts.push(`原因代码：${clean(item.code || task.failureCode)}`);
      for (const [key, label] of [['created', '平台已建单'], ['charged', '平台已扣额度']]) if (typeof item[key] === 'boolean') parts.push(`${label}：${item[key] ? '是' : '否'}`);
      if (Number.isFinite(item.retry_after_seconds)) parts.push(`平台建议 ${item.retry_after_seconds} 秒后重试`);
      for (const [value, label] of [[task.paymentWindowEndsAt, '本轮支付期限'], [task.verificationDeadline, '核验截止时间']]) {
        if (typeof value === 'number' && Number.isFinite(value) && value > 0) parts.push(`${label}：${new Date(value * 1000).toLocaleString()}`);
      }
      if (task.paymentExpiryKind === 'dynamic') parts.push('动态支付期限，以平台重新核验结果为准');
      if (category === 'unknown') parts.push(row.retryAllowed ? '可先查询；若仍未确认，可使用原请求重试。' : '请查询或向服务商核实，不能重新创建此任务。');
      const saved = getAccount(id);
      if (!persisted && saved?.scan_request_status) parts.push(`上次保存状态：${PAYMENT_STATUS[saved.scan_request_status] || clean(saved.scan_request_status)}`);
      row.detail.textContent = parts.filter(Boolean).join(' · ');
      if (persisted) {
        const account = getAccount(id);
        if (account) {
          Object.assign(account, {scan_request_status: row.status, scan_request_provider: row.provider, scan_request_task_id: row.task_id, scan_request_message: clean(item.message || task.message), scan_request_error: clean(item.error || task.error)});
          if (typeof renderAccounts === 'function') renderAccounts();
        }
      }
      summarize();
    }
    function consume(id, data, httpStatus, querying) {
      let found = false;
      let failed = false;
      for (const category of ['created', 'duplicated', 'pending', 'items', 'failed', 'unknown']) {
        for (const item of Array.isArray(data[category]) ? data[category] : []) {
          if (Number(item.id ?? item.account_id) !== id) continue;
          found = true;
          showResult(id, item, category, !querying || !['failed', 'unknown'].includes(category));
          if (category === 'failed' || category === 'unknown' || PAYMENT_FAILED.has(results.get(id).status) || UNCERTAIN.has(results.get(id).status)) failed = true;
        }
      }
      if (!found && httpStatus === 202) {
        showResult(id, {status: 'pending', message: '服务器仍在处理提交，尚未确认创建。请查询最新状态。'}, 'pending');
        found = true;
      }
      if (!found) {
        const error = new Error('服务器未返回该账号的结果，已停止本批次。请查询或刷新账号核实。');
        error.uncertain = true;
        throw error;
      }
      return failed;
    }
    function stopPolling(message = '') {
      clearTimeout(pollTimer);
      pollTimer = null;
      pollStopped = true;
      if (message && state.live()) pollInfo.textContent = message;
    }
    function schedulePolling() {
      if (!state.live() || state.busy || (!credential && !(usesSession() && sessionReady)) || pollStopped) return;
      if (pollExpired()) { stopPolling('本地自动查询已达到 10 分钟，服务器任务没有取消。可手动查询最新状态。'); return; }
      const pending = ids.filter(id => PAYMENT_ACTIVE.has(results.get(id).status));
      if (!pending.length) { stopPolling('自动查询已停止：没有正在处理的任务。可手动查询所选账号。'); return; }
      pollInfo.textContent = `正在跟踪 ${pending.length} 个处理中任务，每 5 秒查询一次。`;
      pollTimer = setTimeout(() => { pollTimer = null; execute('query', pending, true); }, 5000);
    }
    async function paymentRequest(url, body, method = 'POST') {
      const abort = new AbortController();
      controller = abort;
      let timeout;
      try {
        return await Promise.race([
          (async () => {
            const response = await fetch(url, {method, credentials: 'same-origin', headers: {'Accept': 'application/json', ...(body === undefined ? {} : {'Content-Type': 'application/json'})}, ...(body === undefined ? {} : {body: JSON.stringify(body)}), signal: abort.signal});
            let data;
            try { data = await response.json(); } catch (_) { const error = new Error(`服务器响应无法解析（HTTP ${response.status}），请查询核实。`); error.uncertain = true; throw error; }
            if (!response.ok || data?.ok === false) {
              const error = new Error(clean(typeof data?.error === 'string' ? data.error : data?.message || `请求失败（HTTP ${response.status}）`));
              error.uncertain = response.status >= 500 || response.status === 408;
              error.localResponse = true;
              throw error;
            }
            return {data, httpStatus: response.status};
          })(),
          new Promise((_, reject) => {
            timeout = setTimeout(() => {
              const error = new Error('本地等待已超时，服务器任务没有取消。请先查询或刷新核实。');
              error.uncertain = true;
              reject(error);
              abort.abort();
            }, 45000);
          }),
        ]);
      } catch (error) {
        if (error.uncertain == null && !error.localResponse) {
          error = new Error('网络连接中断，本次结果尚未确认。请先查询或刷新核实。');
          error.uncertain = true;
        }
        throw error;
      } finally {
        clearTimeout(timeout);
        if (controller === abort) controller = null;
      }
    }
    async function executeActivation() {
      if (!state.live() || state.busy || attempted.size) return;
      let body;
      try {
        if (ids.some(id => accountLocks.has(id))) throw new Error('所选账号正在提交，请先刷新账号列表核实。');
        const config = extraction.body();
        const payments = activationPayments.body();
        if (!form.reportValidity()) return;
        body = {account_ids: ids, success_group: successGroup.value(), extraction: Array.isArray(config) ? config.map(item => ({...item, link_type: 'upi', payment_amount: 0})) : config, payment: payments};
      } catch (error) { state.fail(error); return; }
      activationSecrets = [...[].concat(body.payment).map(item => item.cdk), ...[].concat(body.extraction).flatMap(item => [item.cdk, item.proxy_url, ...(item.entry_proxies || [])])].filter(Boolean).sort((a, b) => b.length - a.length);
      summarize();
      const configuration = summary.textContent;
      await state.run(async () => {
        ids.forEach(id => { attempted.add(id); accountLocks.add(id); });
        summary.textContent = `${configuration} · 正在提交全部账号…`;
        syncControls();
        try {
          // Serialize once; closing this dialog never aborts the server-side batch.
          const pending = request('/api/accounts/activate-plus', 'POST', body);
          activationPayments.clear(); extraction.clear();
          for (const item of [...[].concat(body.payment), ...[].concat(body.extraction)]) {
            delete item.cdk; delete item.proxy_url; delete item.entry_proxies;
          }
          body = null;
          const data = await pending;
          if (state.live()) {
            form.hidden = true;
            const labels = {started: '已入队', busy: '执行中', skipped: '已跳过', failed: '提交失败'};
            const seen = new Set();
            for (const [category, label] of Object.entries(labels)) {
              for (const item of Array.isArray(data[category]) ? data[category] : []) {
                const id = Number(item.id ?? item.account_id ?? item);
                const result = results.get(id);
                if (!result) continue;
                seen.add(id);
                result.detail.textContent = [label, ACTIVATION_STATUS[item.status], clean(item.reason || item.error || item.message)].filter(Boolean).join(' · ');
                if (category === 'started') {
                  const account = getAccount(id);
                  if (account) Object.assign(account, {plus_activation_status: item.status || 'queued', plus_activation_message: clean(item.message || '开通任务已入队')});
                }
              }
            }
            for (const [id, result] of results) if (!seen.has(id)) result.detail.textContent = '提交结果待核实，请刷新账号列表。';
            summary.textContent = `${configuration} · ` + Object.entries(labels).map(([key, label]) => `${label} ${data[`${key}_count`] ?? (Array.isArray(data[key]) ? data[key].length : 0)}`).join(' · ');
            state.message('请求已处理。后台继续执行；请在账号列表查看进度及真实 Plus 核验结果。');
          }
          await reloadAccounts();
        } catch (error) {
          if (error.uncertain !== false) {
            form.hidden = true;
            summary.textContent = `${configuration} · 提交结果待核实，请刷新账号列表。后台可能已开始处理，本窗口不会重复提交。`;
            for (const result of results.values()) result.detail.textContent = '提交结果待核实';
          } else attempted.clear();
          state.fail(clean(error.message));
        } finally {
          activationPayments.clear(); extraction.clear(); activationSecrets = [];
          ids.forEach(id => accountLocks.delete(id));
        }
      });
      syncControls();
      summarize();
    }
    async function execute(kind, workIds, automatic = false) {
      if (!state.live() || state.busy) return;
      if (automatic && pollExpired()) { stopPolling('本地自动查询已达到 10 分钟，服务器任务没有取消。可手动查询最新状态。'); return; }
      clearTimeout(pollTimer);
      pollTimer = null;
      if (kind !== 'refresh' && usesSession() && !sessionReady) { state.fail('请先登录 OrderHub 或检查登录状态。'); return; }
      if (kind !== 'refresh' && !usesSession() && !credential) {
        if (!form.reportValidity()) { state.fail('请选择已保存的支付 CDK，或输入本次临时凭据。'); picker.focus(); return; }
        try { credential = picker.body(); } catch (error) { state.fail(text(error?.message || error)); picker.focus(); return; }
        credentialSecret = text(credential.cdk || '');
        picker.reset();
      }
      if (!automatic && kind !== 'refresh') pollStartedAt = Date.now();
      if (kind === 'submit' && !attempted.size) selectedProvider = provider.value;
      let encounteredFailure = false;
      await state.run(async () => {
        syncControls();
        if (kind === 'refresh') { await reloadAccounts(); state.message('账号列表已刷新；核实支付任务请点击查询。'); return; }
        for (const id of workIds) {
          if (!state.live() || (automatic && pollExpired())) break;
          const row = results.get(id);
          if (kind === 'submit') {
            if (attempted.has(id) && !retryIds().includes(id)) continue;
            attempted.add(id);
            row.provider = selectedProvider;
          }
          row.category = 'working';
          row.detail.textContent = kind === 'submit' ? '正在提交此账号，请等待服务器响应…' : '正在查询原提交记录…';
          summarize();
          try {
            const body = {account_ids: [id], ...(usesSession() ? {auth_mode: 'session'} : credential)};
            if (kind === 'submit' && selectedProvider === 'orderhub' && !usesSession()) body.auth_mode = 'key';
            if (kind === 'submit') Object.assign(body, {provider: selectedProvider, idempotency_key: batchKey});
            const {data, httpStatus} = await paymentRequest(kind === 'submit' ? '/api/accounts/scan-requests' : '/api/accounts/scan-requests/query', body);
            if (!state.live()) break;
            const failed = consume(id, data, httpStatus, kind === 'query');
            encounteredFailure = encounteredFailure || failed;
            if (row.category === 'unknown' || (automatic && failed)) break;
          } catch (error) {
            if (!state.live()) break;
            encounteredFailure = true;
            const uncertain = error.uncertain !== false;
            showResult(id, {status: uncertain ? 'unknown' : 'failed', error: clean(error.message), can_retry_same_key: kind === 'submit' ? PAYMENT_RETRY.has(selectedProvider) && uncertain : row.retryAllowed}, uncertain ? 'unknown' : 'failed', false);
            state.fail(clean(error.message));
            break;
          }
        }
        if (state.live()) {
          summarize();
          if (encounteredFailure) stopPolling('已停止自动查询。请查看逐账号结果，手动查询或刷新核实；未处理账号需要手动继续。');
          else pollStopped = false;
        }
      });
      syncControls();
      schedulePolling();
    }
    form.addEventListener('submit', event => {
      event.preventDefault();
      if (activation) executeActivation();
      else execute(queryOnly ? 'query' : 'submit', queryOnly ? ids : ids.filter(id => !attempted.has(id)));
    });
    state.dialog.addEventListener('close', () => {
      stopPolling();
      credential = null;
      credentialSecret = '';
      activationSecrets = [];
      extraction?.clear();
      activationPayments?.clear();
      sessionUsername.value = '';
      picker.reset();
      sessionPassword.value = '';
      sessionReady = false;
      controller?.abort();
    });
    summarize();
    syncControls();
    if (activation) return Promise.all([extraction.load(), successGroup.load()]);
    picker.focus();
  }
  async function activatePlus(ids) { return paymentDialog(ids, false, true); }
  async function submitPayment(ids) { paymentDialog(ids, false); }
  async function queryPayment(ids) { paymentDialog(ids, true); }
  function confirmDelete(container, label, action) {
    if (container.querySelector('.el-confirm')) return;
    const row = element('div', {class: 'el-confirm'}, [element('span', {text: label})]);
    row.append(button('确认删除', action, 'el-danger'), button('取消', () => row.remove())); container.append(row); row.querySelector('button').focus();
  }
  const PAYMENT_LABELS = Object.freeze({v1: 'Astra Scan Workbench', masi: 'Masi', orderhub: 'UPI OrderHub', seashore: 'seashore 发布者 API'});
  function paymentManager(state, container) {
    // 支付平台与 CDK 的独立管理：列表只显示掩码，明文永不回到浏览器。
    let providers = [];
    const list = element('div', {class: 'el-provider-list'});
    const editor = element('section', {class: 'el-editor', hidden: true});
    container.append(element('p', {class: 'el-hint', text: '支付平台与 CDK 独立管理：保存后可在提交支付、开通 Plus 时直接选用；明文只留在服务端，列表仅显示脱敏后缀。'}), list, editor);
    async function load() {
      const result = await request('/api/payment-providers'); if (!state.live()) return;
      providers = result.items || []; list.replaceChildren();
      if (!providers.length) list.append(element('p', {text: '尚无支付平台。'}));
      for (const provider of providers) {
        const label = PAYMENT_LABELS[provider.provider_type] || provider.provider_type;
        const card = element('article', {class: 'el-provider'});
        card.append(element('h3', {text: provider.name || label}), element('p', {class: 'el-hint', text: `${label} · ${provider.effective_api_base || ''} · ${enabled(provider.enabled) ? '已启用' : '已停用'}${provider.is_default ? ' · 默认' : ''} · ${(provider.cdks || []).length} 条 CDK`}), element('p', {text: provider.note || ''}));
        card.append(button('编辑 / 管理 CDK', () => editProvider(provider)));
        card.append(button(enabled(provider.enabled) ? '停用平台' : '启用平台', () => state.run(async () => { await request(`/api/payment-providers/${provider.id}`, 'PUT', {enabled: !enabled(provider.enabled)}); editor.hidden = true; await load(); state.message('支付平台状态已更新。'); })));
        card.append(button('删除平台', () => confirmDelete(card, '确认删除此支付平台及其保存的 CDK？', () => state.run(async () => { await request(`/api/payment-providers/${provider.id}`, 'DELETE'); editor.hidden = true; await load(); state.message('支付平台已删除。'); })), 'el-danger'));
        list.append(card);
      }
    }
    function editProvider(provider) {
      editor.replaceChildren(); editor.hidden = false; const form = element('form');
      const name = element('input', {type: 'text', required: '', value: provider?.name || '', maxlength: '120'});
      const type = element('select'); for (const [value, label] of Object.entries(PAYMENT_LABELS)) option(type, value, label); type.value = provider?.provider_type || 'seashore';
      const base = element('input', {type: 'url', value: provider?.api_base || '', placeholder: provider?.effective_api_base || 'https://provider.example'});
      const note = element('textarea', {rows: '2', maxlength: '2000', value: provider?.note || ''});
      const isEnabled = check('启用平台', provider ? enabled(provider.enabled) : true); const isDefault = check('设为默认（提交时仍须手动选择）', !!provider?.is_default);
      form.append(element('fieldset', {class: 'el-fields'}, [field('名称', name), field('平台', type), field('API 地址（留空使用默认）', base), field('备注', note), isEnabled.wrap, isDefault.wrap]));
      form.append(element('button', {type: 'submit', class: 'el-primary', text: '保存平台'}));
      editor.append(element('h3', {text: provider ? `编辑 ${provider.name}` : '新增支付平台'}), form);
      form.addEventListener('submit', event => { event.preventDefault(); if (state.busy || !form.reportValidity()) return; state.run(async () => {
        const value = base.value.trim(); if (value && !safeUrl(value)) throw new Error('API 地址必须是有效的 HTTP 或 HTTPS 地址，且不包含用户名密码。');
        const result = await request(provider ? `/api/payment-providers/${provider.id}` : '/api/payment-providers', provider ? 'PUT' : 'POST', {name: name.value.trim(), provider_type: type.value, api_base: value, enabled: isEnabled.input.checked, is_default: isDefault.input.checked, note: note.value.trim()});
        await load(); const saved = result.item || result; const updated = providers.find(item => Number(item.id) === Number(provider?.id ?? saved.id)); if (updated) editProvider(updated); else editor.hidden = true; state.message('支付平台已保存。');
      }); });
      if (provider) renderCdks(provider);
      editor.scrollIntoView({block: 'nearest'});
    }
    function renderCdks(provider) {
      editor.append(element('h3', {text: '已保存的支付 CDK'})); const cdks = element('div', {class: 'el-cdk-list'});
      if (!(provider.cdks || []).length) cdks.append(element('p', {class: 'el-hint', text: '暂无已保存的 CDK。'}));
      const refreshEditor = async () => { await load(); const updated = providers.find(item => Number(item.id) === Number(provider.id)); if (updated) editProvider(updated); };
      for (const cdk of provider.cdks || []) {
        const card = element('article', {class: 'el-cdk'}); const form = element('form'); const memo = element('input', {type: 'text', value: cdk.memo || '', maxlength: '200'}); const isEnabled = check('启用此 CDK', enabled(cdk.enabled)); const output = element('div', {class: 'el-validation', hidden: true, 'aria-live': 'polite'});
        form.append(element('strong', {text: cdk.masked || masked(cdk)}), field('CDK 备注', memo), isEnabled.wrap, element('button', {type: 'submit', text: '保存备注 / 状态'}));
        form.addEventListener('submit', event => { event.preventDefault(); state.run(async () => { await request(`/api/payment-cdks/${cdk.id}`, 'PUT', {memo: memo.value.trim(), enabled: isEnabled.input.checked}); await refreshEditor(); state.message('支付 CDK 已更新。'); }); });
        card.append(form, button('查询额度 / 统计', () => state.run(async () => balance(output, await request(`/api/payment-cdks/${cdk.id}/validate`, 'POST', {})))), button('删除 CDK', () => confirmDelete(card, `确认删除 ${cdk.masked || masked(cdk)}？`, () => state.run(async () => { await request(`/api/payment-cdks/${cdk.id}`, 'DELETE'); await refreshEditor(); state.message('支付 CDK 已删除。'); })), 'el-danger'), output); cdks.append(card);
      }
      const add = element('form', {class: 'el-cdk-add'}); const raw = element('input', {type: 'password', required: '', autocomplete: 'new-password', spellcheck: 'false'}); const memo = element('input', {type: 'text', maxlength: '200'}); const isEnabled = check('启用新 CDK', true);
      add.append(element('h3', {text: '添加支付 CDK'}), field('新 CDK', raw), field('备注（可选）', memo), isEnabled.wrap, element('button', {type: 'submit', class: 'el-primary', text: '保存新 CDK'}));
      add.addEventListener('submit', event => { event.preventDefault(); if (state.busy || !add.reportValidity()) return; state.run(async () => { if (!raw.value.trim()) throw new Error('请输入支付 CDK。'); await request(`/api/payment-providers/${provider.id}/cdks`, 'POST', {cdk: raw.value.trim(), memo: memo.value.trim(), enabled: isEnabled.input.checked}); raw.value = ''; await refreshEditor(); state.message('新支付 CDK 已保存。'); }); });
      editor.append(cdks, add);
    }
    return {load};
  }
  async function manage() {
    const state = modal('管理提链服务商 / 支付平台与 CDK');
    if (!state) return;
    const toolbar = element('div', {class: 'el-actions'});
    const list = element('div', {class: 'el-provider-list'});
    const editor = element('section', {class: 'el-editor', hidden: true});
    const payments = element('section', {class: 'el-payments', hidden: true});
    const paymentPanel = paymentManager(state, payments);
    state.body.append(element('p', {class: 'el-hint', text: '服务商和 CDK 独立管理，无需选中账号。保存的 CDK 仅显示脱敏后缀；环境服务商为只读。'}), toolbar, list, editor, payments);
    toolbar.append(button('新增服务商', () => editProvider(null)), button('刷新列表', () => state.run(load)), button('管理支付平台 / CDK', () => state.run(async () => { payments.hidden = !payments.hidden; if (!payments.hidden) await paymentPanel.load(); })));
    let providers = [];
    async function load() {
      const result = await request('/api/extract-link/providers'); if (!state.live()) return; providers = result.items || []; list.replaceChildren();
      if (!providers.length) list.append(element('p', {text: '尚无服务商，请新增。'}));
      for (const provider of providers) {
        const card = element('article', {class: 'el-provider'});
        card.append(element('h3', {text: provider.name}), element('p', {class: 'el-hint', text: `${PROVIDER_LABELS[provider.provider_type] || provider.provider_type} · ${provider.api_base || ''} · ${enabled(provider.enabled) ? '已启用' : '已停用'}${provider.is_default ? ' · 配置默认' : ''}`}), element('p', {text: provider.note || ''}));
        card.append(button(Number(provider.id) === 0 ? '查看环境配置' : '编辑 / 管理 CDK', () => editProvider(provider)));
        if (Number(provider.id) !== 0) {
          card.append(button(enabled(provider.enabled) ? '停用服务商' : '启用服务商', () => state.run(async () => { await request(`/api/extract-link/providers/${provider.id}`, 'PUT', {enabled: !enabled(provider.enabled)}); editor.hidden = true; await load(); state.message('服务商状态已更新。'); })));
          card.append(button('删除服务商', () => confirmDelete(card, '确认删除此服务商及其保存的 CDK？', () => state.run(async () => { await request(`/api/extract-link/providers/${provider.id}`, 'DELETE'); editor.hidden = true; await load(); state.message('服务商已删除。'); })), 'el-danger'));
        }
        list.append(card);
      }
    }
    function editProvider(provider) {
      editor.replaceChildren(); editor.hidden = false; const readonly = provider && Number(provider.id) === 0; const form = element('form');
      const name = element('input', {type: 'text', required: '', value: provider?.name || '', maxlength: '120'});
      const type = element('select'); for (const [value, label] of Object.entries(PROVIDER_LABELS)) option(type, value, label); type.value = provider?.provider_type || 'extract';
      const base = element('input', {type: 'url', required: '', value: provider?.api_base || (type.value === 'upi_git5' ? UPI_API_BASE : ''), placeholder: 'https://provider.example'});
      const linkType = element('select'); fillTypes(linkType, type.value, provider?.default_link_type);
      const note = element('textarea', {rows: '2', maxlength: '2000', value: provider?.note || ''});
      const isEnabled = check('启用服务商', provider ? enabled(provider.enabled) : true); const isDefault = check('设为配置默认（提链时仍须手动选择）', !!provider?.is_default);
      const fields = element('fieldset', {class: 'el-fields', disabled: !!readonly}, [field('名称', name), field('服务商类型', type), field('API 地址', base), field('默认支付方式', linkType), field('备注', note), isEnabled.wrap, isDefault.wrap]);
      form.append(fields); editor.append(element('h3', {text: readonly ? '环境服务商（只读）' : provider ? `编辑 ${provider.name}` : '新增服务商'}), form);
      type.addEventListener('change', () => { fillTypes(linkType, type.value); if (type.value === 'upi_git5') base.value = UPI_API_BASE; });
      if (readonly) editor.append(element('p', {class: 'el-notice', text: `该服务商由环境变量配置，请通过服务器配置修改。${provider.has_configured_cdk ? '已有服务器 CDK，提链时可显式选择使用。' : '未配置服务器 CDK。'}`}));
      else {
        form.append(element('button', {type: 'submit', class: 'el-primary', text: '保存服务商'}));
        form.addEventListener('submit', event => { event.preventDefault(); if (state.busy || !form.reportValidity()) return; state.run(async () => { if (!safeUrl(base.value.trim())) throw new Error('API 地址必须是有效的 HTTP 或 HTTPS 地址，且不包含用户名密码。'); const result = await request(provider ? `/api/extract-link/providers/${provider.id}` : '/api/extract-link/providers', provider ? 'PUT' : 'POST', {name: name.value.trim(), provider_type: type.value, api_base: base.value.trim(), default_link_type: linkType.value, enabled: isEnabled.input.checked, is_default: isDefault.input.checked, note: note.value.trim()}); await load(); const saved = result.item || result.provider || result; const updated = providers.find(item => Number(item.id) === Number(provider?.id ?? saved.id)); if (updated) editProvider(updated); else editor.hidden = true; state.message('服务商已保存。'); }); });
      }
      if (provider && !readonly) renderCdks(provider); if (readonly) fields.querySelector('input')?.blur(); else name.focus(); editor.scrollIntoView({block: 'nearest'});
    }
    function renderCdks(provider) {
      editor.append(element('h3', {text: '已保存的 CDK'})); const cdks = element('div', {class: 'el-cdk-list'});
      if (!(provider.cdks || []).length) cdks.append(element('p', {class: 'el-hint', text: '暂无已保存的 CDK。'}));
      const refreshEditor = async () => { await load(); const updated = providers.find(p => Number(p.id) === Number(provider.id)); if (updated) editProvider(updated); };
      for (const cdk of provider.cdks || []) {
        const card = element('article', {class: 'el-cdk'}); const form = element('form'); const memo = element('input', {type: 'text', value: cdk.memo || '', maxlength: '200'}); const isEnabled = check('启用此 CDK', enabled(cdk.enabled)); const output = element('div', {class: 'el-validation', hidden: true, 'aria-live': 'polite'});
        form.append(element('strong', {text: masked(cdk)}), field('CDK 备注', memo), isEnabled.wrap, element('button', {type: 'submit', text: '保存备注 / 状态'}));
        form.addEventListener('submit', event => { event.preventDefault(); state.run(async () => { await request(`/api/extract-link/cdks/${cdk.id}`, 'PUT', {memo: memo.value.trim(), enabled: isEnabled.input.checked}); await refreshEditor(); state.message('CDK 已更新。'); }); });
        card.append(form, button('验证 / 查看余额与费用', () => state.run(async () => balance(output, await request(`/api/extract-link/cdks/${cdk.id}/validate`, 'POST', {})))), button('删除 CDK', () => confirmDelete(card, `确认删除 ${masked(cdk)}？`, () => state.run(async () => { await request(`/api/extract-link/cdks/${cdk.id}`, 'DELETE'); await refreshEditor(); state.message('CDK 已删除。'); })), 'el-danger'), output); cdks.append(card);
      }
      const add = element('form', {class: 'el-cdk-add'}); const raw = element('input', {type: 'password', required: '', autocomplete: 'new-password', spellcheck: 'false'}); const memo = element('input', {type: 'text', maxlength: '200'}); const isEnabled = check('启用新 CDK', true);
      add.append(element('h3', {text: '添加 CDK'}), field('新 CDK', raw), field('备注（可选）', memo), isEnabled.wrap, element('button', {type: 'submit', class: 'el-primary', text: '保存新 CDK'}));
      add.addEventListener('submit', event => { event.preventDefault(); if (state.busy || !add.reportValidity()) return; state.run(async () => { if (!raw.value.trim()) throw new Error('请输入 CDK。'); await request(`/api/extract-link/providers/${provider.id}/cdks`, 'POST', {cdk: raw.value.trim(), memo: memo.value.trim(), enabled: isEnabled.input.checked}); raw.value = ''; await refreshEditor(); state.message('新 CDK 已保存。'); }); }); editor.append(cdks, add);
    }
    await state.run(load);
  }
  function rowButton(id, action, label) { return `<button type="button" class="extract-link-btn" data-el-account="${escape(id)}" data-el-task="${action}">${label}</button>`; }
  function hasSavedTask(row) { return !!(row.extract_link_job_id || (row.extract_link_provider_type === 'upi_git5' && row.extract_link_task_id)); }
  function cell(row) {
    const status = row.extract_link_status || ''; if (!status) return ''; const parts = []; const klass = status === 'success' ? 'status-success' : status === 'failed' ? 'status-failed' : BUSY.has(status) ? 'status-running' : 'status-used';
    parts.push(`<span class="pill ${klass}">${escape(STATUS[status] || status)}</span>`);
    if (row.extract_link_provider_name || row.extract_link_provider_id != null) parts.push(`<div class="el-row-detail">服务商：${escape(row.extract_link_provider_name || '#' + row.extract_link_provider_id)}${row.extract_link_provider_type ? ' · ' + escape(row.extract_link_provider_type) : ''}</div>`);
    if (row.extract_link_type) parts.push(`<div class="el-row-detail">${escape(text(row.extract_link_type).toUpperCase())}</div>`);
    if (row.extract_link_progress != null && row.extract_link_progress !== '') parts.push(`<div class="el-row-detail">进度：${escape(row.extract_link_progress)}</div>`);
    if (row.extract_link_message) parts.push(`<div class="el-row-detail">${escape(row.extract_link_message)}</div>`); if (row.extract_link_error) parts.push(`<div class="extract-link-error">${escape(row.extract_link_error)}</div>`);
    if (row.extract_link_payment_status || status === 'success') parts.push(`<div class="el-row-detail">支付状态：${escape(row.extract_link_payment_status || '尚未确认')}</div>`);
    if (status === 'success') { parts.push('<div class="el-row-detail">Checkout 成功不代表已支付 / 已开通 Plus</div>'); const link = safeUrl(row.extract_link_long_url); if (link) parts.push(`<a class="extract-link-btn" href="${escape(link)}" target="_blank" rel="noopener noreferrer">打开结账链接</a>`); parts.push(rowButton(row.id, 'result', '查看结果 / 复制 / 二维码')); }
    if (row.extract_link_awaiting_blik || status === 'awaiting_blik') parts.push('<div class="el-row-detail">需要提交 6 位 BLIK 验证码</div>'); if (UNCERTAIN.has(status)) parts.push(`<div class="el-row-detail">${hasSavedTask(row) ? '请手动刷新已保存任务；不会自动重试。' : '缺少任务 ID，请先向服务商核实；不可重试。'}</div>`);
    return `<div class="extract-link-cell el-result-cell">${parts.join('')}</div>`;
  }
  function action(row) {
    const status = row.extract_link_status || ''; const parts = []; const upi = row.extract_link_provider_type === 'upi_git5'; const savedTask = hasSavedTask(row);
    if (savedTask) parts.push(rowButton(row.id, 'refresh', upi ? '刷新提链 / 支付状态' : '刷新提链任务'));
    if (upi && savedTask) parts.push(rowButton(row.id, 'qr', '获取 UPI 二维码'));
    if (BUSY.has(status)) { if ((row.extract_link_awaiting_blik || status === 'awaiting_blik' || text(row.extract_link_type).toLowerCase() === 'blik') && row.extract_link_job_id) parts.push(rowButton(row.id, 'blik-code', '输入 BLIK 验证码')); parts.push(rowButton(row.id, 'cancel', '取消提链')); return parts.join(' '); }
    if (UNCERTAIN.has(status)) { if (upi && savedTask) parts.push(rowButton(row.id, 'cancel', '取消提链')); return parts.join(' ') || '<span class="el-row-detail">提链待核实（无任务 ID）</span>'; }
    const plan = text(row.current_plan_type || row.plan_type).toLowerCase(); if (plan === 'free' && row.plus_trial_eligible) parts.push(`<button type="button" class="good" data-extract-link="${escape(row.id)}">${status === 'success' ? '新建提链' : '提链'}</button>`); return parts.join(' ');
  }
  async function copy(value, state) {
    try { if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value); else { const input = element('textarea', {value, class: 'el-copy-buffer', 'aria-label': '待复制的结账数据'}); state.body.append(input); input.select(); const copied = document.execCommand('copy'); input.remove(); if (!copied) throw new Error(); } state.message('已复制。'); } catch (_) { state.fail('复制失败，请手动选择下方结果复制。'); }
  }
  function showResult(state, row, offerQr = true) {
    const output = element('section', {class: 'el-task-result'}); const metadata = {服务商: row.extract_link_provider_name || row.extract_link_provider_id, 服务商类型: PROVIDER_LABELS[row.extract_link_provider_type] || row.extract_link_provider_type, 批次ID: row.extract_link_provider_type === 'upi_git5' ? row.extract_link_task_id : '', 任务ID: row.extract_link_job_id, 状态: STATUS[row.extract_link_status] || row.extract_link_status, 支付状态: row.extract_link_payment_status || '尚未确认', 支付方式: row.extract_link_type, 进度: row.extract_link_progress, 消息: row.extract_link_message, 错误: row.extract_link_error, 到期时间: row.extract_link_expires_at}; const details = element('dl', {class: 'el-details'});
    for (const [key, value] of Object.entries(metadata)) if (value != null && value !== '') details.append(element('dt', {text: key}), element('dd', {text: value})); output.append(details); const link = safeUrl(row.extract_link_long_url); if (link) output.append(element('a', {href: link, target: '_blank', rel: 'noopener noreferrer', text: '打开结账链接'}));
    for (const [label, value] of [['结账链接', row.extract_link_long_url], ['支付复制数据', row.extract_link_copy_paste]]) { if (!value) continue; const data = element('textarea', {readonly: '', rows: '3', value}); output.append(field(label, data), button(`复制${label}`, () => copy(text(value), state))); }
    const qr = safeUrl(row.extract_link_image_url_png, true) || safeUrl(row.extract_link_image_url_svg, true);
    if (qr) { const image = element('img', {class: 'el-qr', alt: '支付二维码，请核实支付状态', referrerpolicy: 'no-referrer', src: qr}); image.addEventListener('error', () => { image.hidden = true; state.fail('二维码图片加载失败，可使用结账链接或复制数据。'); }); output.append(image); }
    if (offerQr && row.extract_link_provider_type === 'upi_git5' && hasSavedTask(row)) output.append(button(qr ? '重新获取 UPI 二维码' : '获取 UPI 二维码', () => task(Number(row.id), 'qr')));
    output.append(element('p', {class: 'el-notice', text: 'Checkout 成功与已支付 / 已开通 Plus 是不同状态。请核实支付结果及账号套餐。'})); const previous = state.body.querySelector('.el-task-result'); if (previous) previous.replaceWith(output); else state.body.append(output);
  }
  function task(id, kind) {
    const row = getAccount(id);
    const titles = {result: '结账结果', 'blik-code': '提交 BLIK 验证码', cancel: '取消提链任务', refresh: '刷新提链 / 支付状态', qr: '获取 UPI 二维码'};
    const state = modal(titles[kind]);
    if (!state) return;
    if (!row) { state.fail('账号不在当前列表中，请刷新账号后重试。'); return; }
    showResult(state, row, kind !== 'qr');
    if (kind === 'result') return;
    if (kind !== 'cancel' && !hasSavedTask(row)) { state.fail('缺少保存的任务或批次 ID，请向服务商核实。不会自动重试。'); return; }
    const upi = row.extract_link_provider_type === 'upi_git5';
    const needsCdk = upi && !(Number(row.extract_link_cdk_id) > 0);
    const form = element('form');
    const cdk = element('input', {type: 'password', autocomplete: 'new-password', spellcheck: 'false', required: needsCdk});
    const code = element('input', {type: 'text', inputmode: 'numeric', pattern: '[0-9]{6}', minlength: '6', maxlength: '6', required: '', autocomplete: 'one-time-code'});
    if (needsCdk) form.append(field('本次任务的提链 CDK', cdk, '请重新输入提交此任务时使用的一次性 CDK，用于刷新、取消或获取二维码。请求结束后清空，登录会话由服务器管理。'));
    else if (upi) form.append(element('p', {class: 'el-hint', text: '服务器将自动使用此任务关联的已保存 CDK。'}));
    if (kind === 'blik-code') form.append(field('BLIK 验证码（6 位数字）', code, '从支付应用获取当前验证码，提交后请在应用中核实付款。'));
    if (kind === 'cancel') form.append(element('p', {class: 'el-notice', text: '确认请求取消当前任务？已经完成的支付不能通过此操作撤销。'}));
    const submit = element('button', {type: 'submit', class: kind === 'cancel' ? 'el-danger' : 'el-primary', text: kind === 'blik-code' ? '提交验证码' : kind === 'cancel' ? '确认取消任务' : kind === 'qr' ? '获取二维码' : '手动刷新任务'});
    form.append(submit); state.body.append(form);
    if (needsCdk) cdk.focus(); else if (kind === 'blik-code') code.focus(); else submit.focus();
    form.addEventListener('submit', event => {
      event.preventDefault();
      if (state.busy || !form.reportValidity()) return;
      state.run(async () => {
        if (accountLocks.has(id)) throw new Error('该账号操作正在进行，请稍候。');
        if (kind === 'blik-code' && !/^[0-9]{6}$/.test(code.value)) throw new Error('请输入 6 位数字的 BLIK 验证码。');
        if (needsCdk && !cdk.value.trim()) throw new Error('请输入提交此任务时使用的提链 CDK。');
        const body = kind === 'blik-code' ? {blik_code: code.value} : {};
        if (needsCdk) body.cdk = cdk.value.trim();
        accountLocks.add(id);
        try {
          const result = await request(`/api/accounts/${id}/extract-link/${kind}`, 'POST', body);
          code.value = '';
          if (kind === 'qr') {
            const imageUrl = safeUrl(result.image_url_png, true);
            if (!imageUrl) throw new Error('服务器未返回有效的二维码图片，请稍后刷新任务核实。');
            showResult(state, {...row, ...(result.account || result.item || {}), extract_link_image_url_png: imageUrl}, false);
            state.message(result.message || '二维码已获取，请核实支付状态。');
          } else {
            await reloadAccounts();
            const latest = result.account || result.item || getAccount(id) || row;
            showResult(state, latest);
            if (kind !== 'refresh') form.hidden = true;
            state.message(result.message || '操作完成，已刷新账号状态。');
          }
        } catch (err) {
          if (err.uncertain && !['refresh', 'qr'].includes(kind)) form.hidden = true;
          throw err;
        } finally { cdk.value = ''; accountLocks.delete(id); }
      });
    });
  }
  document.addEventListener('click', event => { const manager = event.target.closest('[data-extract-manage]'); if (manager) { event.preventDefault(); manage(); return; } const target = event.target.closest('[data-el-task]'); if (!target) return; event.preventDefault(); const id = Number(target.dataset.elAccount); const kind = target.dataset.elTask; if (Number.isSafeInteger(id) && ['result', 'refresh', 'cancel', 'blik-code', 'qr'].includes(kind)) task(id, kind); });
  window.ExtractLinks = Object.freeze({open, manage, activatePlus, activationCell, submitPayment, queryPayment, paymentCell, cell, action});
  if (typeof renderAccounts === 'function' && typeof ACCOUNTS !== 'undefined' && ACCOUNTS.length) renderAccounts();
})();
