(() => {
  'use strict';
  let active = null;

  function open({accountIds, currentGroup = '', request, onSaved}) {
    if (active) { active.focus(); return; }
    const ids = [...accountIds];
    if (!ids.length) return;
    const opener = document.activeElement;
    const overlay = document.createElement('div');
    overlay.className = 'account-group-overlay';
    overlay.innerHTML = `
      <section class="account-group-panel" role="dialog" aria-modal="true" aria-labelledby="accountGroupTitle" aria-describedby="accountGroupDescription" tabindex="-1">
        <div class="account-group-header">
          <h2 id="accountGroupTitle">设置分组</h2>
          <button type="button" class="account-group-close" aria-label="关闭设置分组">×</button>
        </div>
        <p id="accountGroupDescription"></p>
        <input class="account-group-search" type="search" placeholder="搜索已有分组" aria-label="搜索已有分组" autocomplete="off" disabled>
        <div class="account-group-list" role="radiogroup" aria-label="已有分组" aria-busy="true"></div>
        <p class="account-group-status" role="status">正在加载分组…</p>
        <p class="account-group-error" role="alert" hidden></p>
        <button type="button" class="account-group-retry" hidden>重新加载</button>
        <div class="account-group-footer">
          <span class="account-group-selection">请选择一个分组</span>
          <button type="button" class="account-group-cancel">取消</button>
          <button type="button" class="account-group-save" disabled>确认分组</button>
        </div>
      </section>`;
    const panel = overlay.querySelector('.account-group-panel');
    const search = overlay.querySelector('.account-group-search');
    const list = overlay.querySelector('.account-group-list');
    const status = overlay.querySelector('.account-group-status');
    const error = overlay.querySelector('.account-group-error');
    const retry = overlay.querySelector('.account-group-retry');
    const save = overlay.querySelector('.account-group-save');
    const cancel = overlay.querySelector('.account-group-cancel');
    const closeButton = overlay.querySelector('.account-group-close');
    const selection = overlay.querySelector('.account-group-selection');
    overlay.querySelector('#accountGroupDescription').textContent = `将 ${ids.length} 个账号移入所选分组。每个账号只能属于一个分组。`;
    let groups = [];
    let selected = '';
    let busy = false;
    const background = Array.from(document.body.children).map(node => [node, node.inert]);
    const state = {focus: () => panel.focus()};
    active = state;
    document.body.append(overlay);
    background.forEach(([node]) => { node.inert = true; });
    window.updateModalScrollLock?.();
    panel.focus();

    function close() {
      if (busy || active !== state) return;
      active = null;
      overlay.remove();
      background.forEach(([node, inert]) => { node.inert = inert; });
      window.updateModalScrollLock?.();
      if (opener?.isConnected && !opener.disabled) opener.focus();
    }

    function showError(message) {
      error.textContent = message;
      error.hidden = !message;
    }

    function filter() {
      const query = search.value.trim().toLocaleLowerCase();
      let visible = 0;
      list.querySelectorAll('.account-group-option').forEach(row => {
        row.hidden = !row.dataset.name.toLocaleLowerCase().includes(query);
        if (!row.hidden) visible++;
      });
      status.textContent = visible ? `共 ${visible} 个分组，请勾选一个` : groups.length ? '没有匹配的分组，请更换搜索词。' : '暂无已有分组，请先在分组管理中创建。';
    }

    async function load() {
      retry.hidden = true;
      showError('');
      status.textContent = '正在加载分组…';
      list.setAttribute('aria-busy', 'true');
      try {
        const result = await request('/api/account-groups', {cache: 'no-store'});
        if (active !== state) return;
        if (!Array.isArray(result.groups) || result.groups.some(group => typeof group?.group_name !== 'string' || !group.group_name.trim())) {
          throw new Error('分组列表格式无效');
        }
        groups = result.groups;
        list.replaceChildren();
        for (const group of groups) {
          const row = document.createElement('label');
          row.className = 'account-group-option';
          row.dataset.name = group.group_name;
          const radio = document.createElement('input');
          radio.type = 'radio';
          radio.name = 'account-group-choice';
          radio.value = group.group_name;
          radio.checked = group.group_name === currentGroup;
          if (radio.checked) selected = group.group_name;
          const name = document.createElement('span');
          name.className = 'account-group-name';
          name.textContent = group.group_name;
          const count = document.createElement('span');
          count.className = 'account-group-count';
          count.textContent = `${Number(group.total) || 0} 个账号`;
          row.append(radio, name, count);
          list.append(row);
        }
        search.disabled = !groups.length;
        save.disabled = !selected;
        selection.textContent = selected ? `已选：${selected}` : '请选择一个分组';
        filter();
      } catch (err) {
        if (active !== state) return;
        status.textContent = '';
        showError('加载分组失败：' + err.message);
        retry.hidden = false;
      } finally {
        if (active === state) list.setAttribute('aria-busy', 'false');
      }
    }

    list.addEventListener('change', event => {
      if (busy || !event.target.matches('input[type="radio"]')) return;
      selected = event.target.value;
      selection.textContent = `已选：${selected}`;
      save.disabled = false;
      showError('');
    });
    search.addEventListener('input', filter);
    retry.addEventListener('click', load);
    closeButton.addEventListener('click', close);
    cancel.addEventListener('click', close);
    overlay.addEventListener('click', event => { if (event.target === overlay) close(); });
    overlay.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); }
      if (event.key !== 'Tab') return;
      const controls = Array.from(panel.querySelectorAll('button, input')).filter(node => !node.disabled && node.getClientRects().length);
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (!first) { event.preventDefault(); panel.focus(); return; }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === panel)) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === panel)) {
        event.preventDefault(); first.focus();
      }
    });
    save.addEventListener('click', async () => {
      if (busy || !groups.some(group => group.group_name === selected)) return;
      busy = true;
      showError('');
      panel.setAttribute('aria-busy', 'true');
      const controls = Array.from(panel.querySelectorAll('button, input')).map(node => [node, node.disabled]);
      controls.forEach(([node]) => { node.disabled = true; });
      save.textContent = '保存中…';
      panel.focus();
      let result;
      try {
        result = await request('/api/accounts/group-bulk', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({account_ids: ids, group_name: selected}),
        });
      } catch (err) {
        showError('设置分组失败：' + err.message);
      } finally {
        busy = false;
        panel.removeAttribute('aria-busy');
        controls.forEach(([node, disabled]) => { node.disabled = disabled; });
        save.textContent = '确认分组';
      }
      if (result) {
        close();
        onSaved(result, selected);
      } else {
        save.focus();
      }
    });
    load();
  }

  window.AccountGroupPicker = {open};
})();
