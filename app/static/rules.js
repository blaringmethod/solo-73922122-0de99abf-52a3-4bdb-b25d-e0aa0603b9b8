// Subject-rules page: rule CRUD against JSON endpoints, live preview, and an
// optimistic-concurrency confirm.  Every mutation refetches the preview so the
// page always reflects the rules currently in force.
(() => {
  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));

  const messageEl = document.getElementById("rule-message");
  const rulesBody = document.querySelector("#rules-table tbody");
  const versionStamp = document.getElementById("version-stamp");
  const confirmBtn = document.getElementById("btn-confirm");
  const dialog = document.getElementById("rule-dialog");
  const form = document.getElementById("rule-form");

  function flash(text, kind = "error") {
    messageEl.textContent = text;
    messageEl.className = "flash show " + kind;
  }

  function clearFlash() {
    messageEl.className = "flash";
    messageEl.textContent = "";
  }

  function renderRules(rules) {
    if (!rules.length) {
      rulesBody.innerHTML =
        '<tr><td colspan="4" class="empty">还没有规则。新增规则后可预览对已发布书目的影响。</td></tr>';
      return;
    }
    rulesBody.innerHTML = rules
      .map(
        (rule) => `
        <tr class="rule-row" data-rule-id="${rule.id}"
            data-keyword="${esc(rule.keyword)}" data-subject="${esc(rule.subject)}"
            data-priority="${esc(rule.priority)}">
          <td>${esc(rule.keyword)}</td>
          <td>${esc(rule.subject)}</td>
          <td class="mono">${esc(rule.priority)}</td>
          <td class="rule-actions">
            <button type="button" class="ghost js-edit">修改</button>
            <button type="button" class="danger js-delete">删除</button>
          </td>
        </tr>`
      )
      .join("");
  }

  function renderPreview(preview) {
    versionStamp.textContent =
      `规则版本 v${preview.rules_version} · 目录版本 v${preview.catalog_version}`;
    document.getElementById("sum-total").textContent = preview.catalog_size;
    document.getElementById("sum-assigned").textContent = preview.assigned_count;
    document.getElementById("sum-changed").textContent = preview.would_change_count;
    document.getElementById("sum-conflicts").textContent = preview.conflict_count;

    const conflictBox = document.getElementById("conflict-box");
    conflictBox.hidden = preview.conflict_count === 0;
    document.querySelector("#conflicts-table tbody").innerHTML = preview.conflicts
      .map(
        (row) => `
        <tr>
          <td>${esc(row.title)}</td>
          <td>${row.conflict_subjects
            .map((s) => `<span class="conflict-tag">${esc(s)}</span>`)
            .join(" ")}</td>
          <td class="mono small">${esc(row.matched_rule_ids.join(", "))}</td>
        </tr>`
      )
      .join("");

    const changesBody = document.querySelector("#changes-table tbody");
    changesBody.innerHTML = preview.changes.length
      ? preview.changes
          .map(
            (row) => `
          <tr>
            <td class="mono small">${esc(row.identifier)}</td>
            <td>${esc(row.title)}</td>
            <td>${row.current_subject ? esc(row.current_subject) : '<span class="muted">（空）</span>'}</td>
            <td><span class="subject-tag">${esc(row.subject)}</span></td>
            <td class="mono small">${esc(row.matched_rule_ids.join(", "))}</td>
          </tr>`
          )
          .join("")
      : '<tr><td colspan="5" class="empty">没有主题会发生变化。</td></tr>';

    confirmBtn.disabled = preview.conflict_count > 0;
    confirmBtn.title = preview.conflict_count
      ? "存在冲突，须先调整规则"
      : "";
    return preview;
  }

  async function loadPreview() {
    const response = await fetch("/api/rules", { cache: "no-store" });
    if (!response.ok) throw new Error("预览加载失败 HTTP " + response.status);
    const payload = await response.json();
    renderRules(payload.rules);
    return renderPreview(payload);
  }

  async function postForm(url, data) {
    const body = new URLSearchParams();
    for (const [key, value] of Object.entries(data)) {
      if (value !== null && value !== "") body.set(key, value);
    }
    return fetch(url, { method: "POST", body });
  }

  // ------------------------------------------------------------------- dialog
  document.getElementById("btn-new-rule").addEventListener("click", () => {
    clearFlash();
    document.getElementById("dialog-title").textContent = "新增规则";
    form.reset();
    document.getElementById("f-id").value = "";
    dialog.showModal();
  });

  rulesBody.addEventListener("click", (event) => {
    const editBtn = event.target.closest(".js-edit");
    const deleteBtn = event.target.closest(".js-delete");
    const row = event.target.closest(".rule-row");
    if (!row) return;

    if (editBtn) {
      clearFlash();
      document.getElementById("dialog-title").textContent = "修改规则";
      document.getElementById("f-id").value = row.dataset.ruleId;
      document.getElementById("f-keyword").value = row.dataset.keyword;
      document.getElementById("f-subject").value = row.dataset.subject;
      document.getElementById("f-priority").value = row.dataset.priority;
      dialog.showModal();
    } else if (deleteBtn) {
      const id = row.dataset.ruleId;
      if (!confirm(`删除规则「${row.dataset.keyword} → ${row.dataset.subject}」？`)) return;
      void (async () => {
        try {
          const response = await fetch(`/api/rules/${id}`, { method: "DELETE" });
          const payload = await response.json().catch(() => ({}));
          if (!response.ok) {
            flash(payload.detail || "删除失败");
            return;
          }
          renderRules(payload.preview.rules);
          renderPreview(payload.preview);
          flash("规则已删除（未确认前不改变已发布主题）", "success");
        } catch (error) {
          flash("请求失败：" + error.message);
        }
      })();
    }
  });

  document.getElementById("dialog-cancel").addEventListener("click", () => dialog.close());

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const priorityRaw = document.getElementById("f-priority").value;
    if (!/^[+-]?\d+$/.test(priorityRaw.trim())) {
      flash("优先级必须是整数");
      return;
    }
    const data = {
      id: document.getElementById("f-id").value,
      keyword: document.getElementById("f-keyword").value,
      subject: document.getElementById("f-subject").value,
      priority: priorityRaw,
    };
    void (async () => {
      try {
        const response = await postForm("/api/rules", data);
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          flash(payload.detail || "保存失败");
          return;
        }
        dialog.close();
        renderRules(payload.preview.rules);
        renderPreview(payload.preview);
        flash("规则已保存（未确认前不改变已发布主题），请查看预览", "success");
      } catch (error) {
        flash("请求失败：" + error.message);
      }
    })();
  });

  // ------------------------------------------------------------------ confirm
  confirmBtn.addEventListener("click", () => {
    if (confirmBtn.disabled) return;
    void (async () => {
      try {
        // The versions come from the preview currently rendered: the server
        // rejects them if rules or catalogue changed in the meantime.
        const stamp = versionStamp.textContent.match(/v(\d+)/g) || [];
        const rulesVersion = Number((stamp[0] || "v0").slice(1));
        const catalogVersion = Number((stamp[1] || "v0").slice(1));
        const body = new URLSearchParams({
          rules_version: String(rulesVersion),
          catalog_version: String(catalogVersion),
        });
        const response = await fetch("/api/rules/confirm", { method: "POST", body });
        const payload = await response.json().catch(() => ({}));
        if (response.ok && payload.confirmed) {
          const preview = await loadPreview();
          flash(
            `已确认：${preview.catalog_size} 条书目的主题已按当前规则更新`,
            "success"
          );
          return;
        }
        if (payload.current) {
          renderRules(payload.current.rules);
          renderPreview(payload.current);
        }
        const detail =
          payload.reason === "stale"
            ? "预览后规则或目录已变化，旧确认已拒绝；已展示当前预览，请重新确认"
            : payload.detail || "确认被拒绝";
        flash(detail);
      } catch (error) {
        flash("请求失败：" + error.message);
      }
    })();
  });
})();
