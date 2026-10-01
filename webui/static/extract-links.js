/* Shared checkout UI. Credentials stay in the current form or on the server. */
(() => {
  'use strict';
  const TYPES = Object.freeze({
    extract: ['pix', 'upi', 'kakao_pay', 'ideal'],
    lumen: ['ideal', 'upi', 'pix', 'paypal', 'kakao_pay', 'momo', 'blik', 'twint', 'gcash', 'gopay'],
  });
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
    if (!response.ok || data.ok === false) {
      const reason = data.error || data.message || `HTTP ${response.status}`;
      const error = new Error(typeof reason === 'string' ? reason : JSON.stringify(reason));
      error.payload = data;
      error.uncertain = method !== 'GET' && response.status >= 500;
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
      if (state.busy) { event.preventDefault(); state.message('正在等待服务器响应，请稍候再关闭。'); }
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
      const controls = Array.from(dialog.querySelectorAll('button, input, select, textarea, fieldset'));
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
    const valid = result.valid ?? result.data?.valid ?? result.balance?.valid;
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
    const providerInfo = element('p', {class: 'el-hint'});
    const validation = element('div', {class: 'el-validation', hidden: true, 'aria-live': 'polite'});
    const validate = button('验证 CDK / 查看余额与费用', () => state.run(async () => balance(validation, await request('/api/extract-link/cdk', 'POST', credentialBody()))));
    const submit = element('button', {type: 'submit', class: 'el-primary', text: `提交 ${ids.length} 个账号的提链任务`});
    fields.append(field('服务商（必选）', providerSelect), providerInfo, field('支付方式', typeSelect), field('CDK 来源（必选）', credentials), rawSection, proxySection, validate, validation);
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
      raw.value = ''; memo.value = ''; proxy.value = ''; save.input.checked = false; raw.required = false; rawSection.hidden = true; validation.hidden = true; credentials.replaceChildren(); option(credentials, '', '请选择 CDK 来源'); typeSelect.replaceChildren(); option(typeSelect, '', '请先选择服务商'); proxySection.hidden = true; providerInfo.textContent = '';
      if (providerSelect.value === '') return;
      const selected = provider();
      fillTypes(typeSelect, selected.provider_type, selected.default_link_type);
      providerInfo.textContent = `${selected.name} · ${selected.provider_type === 'lumen' ? 'Lumen Flow' : 'Extract'}${Number(selected.id) === 0 ? ' · 环境配置（只读）' : ''}${selected.note ? ' · ' + selected.note : ''}`;
      for (const cdk of selected.cdks || []) if (enabled(cdk.enabled)) option(credentials, `saved:${cdk.id}`, `${masked(cdk)}${cdk.memo ? ' · ' + cdk.memo : ''}`);
      if (Number(selected.id) === 0 && selected.has_configured_cdk) option(credentials, 'configured', '使用服务器环境配置的 CDK');
      option(credentials, 'raw', '输入新 CDK（仅本次使用，保存需勾选）');
      save.wrap.hidden = Number(selected.id) === 0; memo.parentElement.hidden = Number(selected.id) === 0; proxySection.hidden = selected.provider_type !== 'lumen';
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
        if (selected.provider_type === 'lumen' && proxy.value.trim()) body.options = {proxyUrl: proxy.value.trim()};
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
          raw.value = ''; proxy.value = ''; form.hidden = true;
          const skipped = Number(result.skipped_count || 0) + Number(result.busy_count || 0) + Number(result.failed_count || 0);
          state.message(result.message || (single ? '提链任务已提交。请在账号列表查看进度。' : `已入队 ${result.started_count || 0} 个；跳过 / 失败 ${skipped} 个。`));
          if (skipped || result.errors?.length || result.skipped?.length) state.body.append(element('pre', {class: 'el-json', text: JSON.stringify(redact(result), null, 2)}));
          await reloadAccounts();
        } catch (err) {
          if (err.uncertain) { form.hidden = true; raw.value = ''; proxy.value = ''; state.body.append(button('刷新账号以核实结果', () => state.run(reloadAccounts))); }
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
        for (const item of providers) if (enabled(item.enabled)) option(providerSelect, item.id, `${item.name} · ${item.provider_type === 'lumen' ? 'Lumen Flow' : 'Extract'}${item.is_default ? '（配置默认；仍需手动选择）' : ''}`);
        fields.disabled = false; submit.disabled = false; selectProvider(); providerSelect.focus();
        if (!providers.some(p => enabled(p.enabled))) state.fail('尚无启用的服务商，请先管理服务商并配置 CDK。');
      } catch (err) {
        if (!state.live()) return;
        state.fail(err); state.body.append(button('重新加载服务商', async event => { event.currentTarget.remove(); await loadProviders(); }));
      }
    }
    await loadProviders();
  }
  function confirmDelete(container, label, action) {
    if (container.querySelector('.el-confirm')) return;
    const row = element('div', {class: 'el-confirm'}, [element('span', {text: label})]);
    row.append(button('确认删除', action, 'el-danger'), button('取消', () => row.remove())); container.append(row); row.querySelector('button').focus();
  }
  async function manage() {
    const state = modal('管理提链服务商 / CDK');
    if (!state) return;
    const toolbar = element('div', {class: 'el-actions'});
    const list = element('div', {class: 'el-provider-list'});
    const editor = element('section', {class: 'el-editor', hidden: true});
    state.body.append(element('p', {class: 'el-hint', text: '服务商和 CDK 独立管理，无需选中账号。保存的 CDK 仅显示脱敏后缀；环境服务商为只读。'}), toolbar, list, editor);
    toolbar.append(button('新增服务商', () => editProvider(null)), button('刷新列表', () => state.run(load)));
    let providers = [];
    async function load() {
      const result = await request('/api/extract-link/providers'); if (!state.live()) return; providers = result.items || []; list.replaceChildren();
      if (!providers.length) list.append(element('p', {text: '尚无服务商，请新增。'}));
      for (const provider of providers) {
        const card = element('article', {class: 'el-provider'});
        card.append(element('h3', {text: provider.name}), element('p', {class: 'el-hint', text: `${provider.provider_type} · ${provider.api_base || ''} · ${enabled(provider.enabled) ? '已启用' : '已停用'}${provider.is_default ? ' · 配置默认' : ''}`}), element('p', {text: provider.note || ''}));
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
      const type = element('select'); option(type, 'extract', 'Extract'); option(type, 'lumen', 'Lumen Flow'); type.value = provider?.provider_type || 'extract';
      const base = element('input', {type: 'url', required: '', value: provider?.api_base || '', placeholder: 'https://provider.example'});
      const linkType = element('select'); fillTypes(linkType, type.value, provider?.default_link_type);
      const note = element('textarea', {rows: '2', maxlength: '2000', value: provider?.note || ''});
      const isEnabled = check('启用服务商', provider ? enabled(provider.enabled) : true); const isDefault = check('设为配置默认（提链时仍须手动选择）', !!provider?.is_default);
      const fields = element('fieldset', {class: 'el-fields', disabled: !!readonly}, [field('名称', name), field('服务商类型', type), field('API 地址', base), field('默认支付方式', linkType), field('备注', note), isEnabled.wrap, isDefault.wrap]);
      form.append(fields); editor.append(element('h3', {text: readonly ? '环境服务商（只读）' : provider ? `编辑 ${provider.name}` : '新增服务商'}), form);
      type.addEventListener('change', () => fillTypes(linkType, type.value));
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
  function cell(row) {
    const status = row.extract_link_status || ''; if (!status) return ''; const parts = []; const klass = status === 'success' ? 'status-success' : status === 'failed' ? 'status-failed' : BUSY.has(status) ? 'status-running' : 'status-used';
    parts.push(`<span class="pill ${klass}">${escape(STATUS[status] || status)}</span>`);
    if (row.extract_link_provider_name || row.extract_link_provider_id != null) parts.push(`<div class="el-row-detail">服务商：${escape(row.extract_link_provider_name || '#' + row.extract_link_provider_id)}${row.extract_link_provider_type ? ' · ' + escape(row.extract_link_provider_type) : ''}</div>`);
    if (row.extract_link_type) parts.push(`<div class="el-row-detail">${escape(text(row.extract_link_type).toUpperCase())}</div>`);
    if (row.extract_link_progress != null && row.extract_link_progress !== '') parts.push(`<div class="el-row-detail">进度：${escape(row.extract_link_progress)}</div>`);
    if (row.extract_link_message) parts.push(`<div class="el-row-detail">${escape(row.extract_link_message)}</div>`); if (row.extract_link_error) parts.push(`<div class="extract-link-error">${escape(row.extract_link_error)}</div>`);
    if (row.extract_link_payment_status || status === 'success') parts.push(`<div class="el-row-detail">支付状态：${escape(row.extract_link_payment_status || '尚未确认')}</div>`);
    if (status === 'success') { parts.push('<div class="el-row-detail">Checkout 成功不代表已支付 / 已开通 Plus</div>'); const link = safeUrl(row.extract_link_long_url); if (link) parts.push(`<a class="extract-link-btn" href="${escape(link)}" target="_blank" rel="noopener noreferrer">打开结账链接</a>`); parts.push(rowButton(row.id, 'result', '查看结果 / 复制 / 二维码')); }
    if (row.extract_link_awaiting_blik || status === 'awaiting_blik') parts.push('<div class="el-row-detail">需要提交 6 位 BLIK 验证码</div>'); if (UNCERTAIN.has(status)) parts.push(`<div class="el-row-detail">${row.extract_link_job_id ? '请手动刷新已保存任务；不会自动重试。' : '缺少任务 ID，请先向服务商核实；不可重试。'}</div>`);
    return `<div class="extract-link-cell el-result-cell">${parts.join('')}</div>`;
  }
  function action(row) {
    const status = row.extract_link_status || ''; const parts = []; if (row.extract_link_job_id) parts.push(rowButton(row.id, 'refresh', '刷新提链任务'));
    if (BUSY.has(status)) { if ((row.extract_link_awaiting_blik || status === 'awaiting_blik' || text(row.extract_link_type).toLowerCase() === 'blik') && row.extract_link_job_id) parts.push(rowButton(row.id, 'blik-code', '输入 BLIK 验证码')); parts.push(rowButton(row.id, 'cancel', '取消提链')); return parts.join(' '); }
    if (UNCERTAIN.has(status)) return parts.join(' ') || '<span class="el-row-detail">提链待核实（无任务 ID）</span>';
    const plan = text(row.current_plan_type || row.plan_type).toLowerCase(); if (plan === 'free' && row.plus_trial_eligible) parts.push(`<button type="button" class="good" data-extract-link="${escape(row.id)}">${status === 'success' ? '新建提链' : '提链'}</button>`); return parts.join(' ');
  }
  async function copy(value, state) {
    try { if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value); else { const input = element('textarea', {value, class: 'el-copy-buffer', 'aria-label': '待复制的结账数据'}); state.body.append(input); input.select(); const copied = document.execCommand('copy'); input.remove(); if (!copied) throw new Error(); } state.message('已复制。'); } catch (_) { state.fail('复制失败，请手动选择下方结果复制。'); }
  }
  function showResult(state, row) {
    const output = element('section', {class: 'el-task-result'}); const metadata = {服务商: row.extract_link_provider_name || row.extract_link_provider_id, 服务商类型: row.extract_link_provider_type, 任务ID: row.extract_link_job_id, 状态: STATUS[row.extract_link_status] || row.extract_link_status, 支付状态: row.extract_link_payment_status || '尚未确认', 支付方式: row.extract_link_type, 进度: row.extract_link_progress, 消息: row.extract_link_message, 错误: row.extract_link_error, 到期时间: row.extract_link_expires_at}; const details = element('dl', {class: 'el-details'});
    for (const [key, value] of Object.entries(metadata)) if (value != null && value !== '') details.append(element('dt', {text: key}), element('dd', {text: value})); output.append(details); const link = safeUrl(row.extract_link_long_url); if (link) output.append(element('a', {href: link, target: '_blank', rel: 'noopener noreferrer', text: '打开结账链接'}));
    for (const [label, value] of [['结账链接', row.extract_link_long_url], ['支付复制数据', row.extract_link_copy_paste]]) { if (!value) continue; const data = element('textarea', {readonly: '', rows: '3', value}); output.append(field(label, data), button(`复制${label}`, () => copy(text(value), state))); }
    const qr = safeUrl(row.extract_link_image_url_png, true) || safeUrl(row.extract_link_image_url_svg, true); if (qr) { const image = element('img', {class: 'el-qr', alt: '支付二维码，请核实支付状态', referrerpolicy: 'no-referrer', src: qr}); image.addEventListener('error', () => { image.hidden = true; state.fail('二维码图片加载失败，可使用结账链接或复制数据。'); }); output.append(image); } output.append(element('p', {class: 'el-notice', text: 'Checkout 成功与已支付 / 已开通 Plus 是不同状态。请核实支付结果及账号套餐。'})); const previous = state.body.querySelector('.el-task-result'); if (previous) previous.replaceWith(output); else state.body.append(output);
  }
  function task(id, kind) {
    const row = getAccount(id); const state = modal(kind === 'result' ? '结账结果' : kind === 'blik-code' ? '提交 BLIK 验证码' : kind === 'cancel' ? '取消提链任务' : '刷新提链任务'); if (!state) return; if (!row) { state.fail('账号不在当前列表中，请刷新账号后重试。'); return; } showResult(state, row); if (kind === 'result') return; if (kind !== 'cancel' && !row.extract_link_job_id) { state.fail('缺少保存的任务 ID，请向服务商核实。不会自动重试。'); return; }
    const form = element('form'); const code = element('input', {type: 'text', inputmode: 'numeric', pattern: '[0-9]{6}', minlength: '6', maxlength: '6', required: '', autocomplete: 'one-time-code'}); if (kind === 'blik-code') form.append(field('BLIK 验证码（6 位数字）', code, '从支付应用获取当前验证码，提交后请在应用中核实付款。')); if (kind === 'cancel') form.append(element('p', {class: 'el-notice', text: '确认请求取消当前任务？已经完成的支付不能通过此操作撤销。'})); const submit = element('button', {type: 'submit', class: kind === 'cancel' ? 'el-danger' : 'el-primary', text: kind === 'blik-code' ? '提交验证码' : kind === 'cancel' ? '确认取消任务' : '手动刷新任务'}); form.append(submit); state.body.append(form); if (kind === 'blik-code') code.focus(); else submit.focus();
    form.addEventListener('submit', event => { event.preventDefault(); if (state.busy || !form.reportValidity()) return; state.run(async () => { if (accountLocks.has(id)) throw new Error('该账号操作正在进行，请稍候。'); if (kind === 'blik-code' && !/^[0-9]{6}$/.test(code.value)) throw new Error('请输入 6 位数字的 BLIK 验证码。'); accountLocks.add(id); try { const result = await request(`/api/accounts/${id}/extract-link/${kind}`, 'POST', kind === 'blik-code' ? {blikCode: code.value} : {}); code.value = ''; await reloadAccounts(); const latest = result.account || result.item || getAccount(id) || row; showResult(state, latest); if (kind !== 'refresh') form.hidden = true; state.message(result.message || '操作完成，已刷新账号状态。'); } catch (err) { if (err.uncertain && kind !== 'refresh') form.hidden = true; throw err; } finally { accountLocks.delete(id); } }); });
  }
  document.addEventListener('click', event => { const manager = event.target.closest('[data-extract-manage]'); if (manager) { event.preventDefault(); manage(); return; } const target = event.target.closest('[data-el-task]'); if (!target) return; event.preventDefault(); const id = Number(target.dataset.elAccount); const kind = target.dataset.elTask; if (Number.isSafeInteger(id) && ['result', 'refresh', 'cancel', 'blik-code'].includes(kind)) task(id, kind); });
  window.ExtractLinks = Object.freeze({open, manage, cell, action});
  if (typeof renderAccounts === 'function' && typeof ACCOUNTS !== 'undefined' && ACCOUNTS.length) renderAccounts();
})();
