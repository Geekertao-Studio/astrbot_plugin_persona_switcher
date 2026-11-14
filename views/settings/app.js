const bridge = window.AstrBotPluginView;

const els = {
  badge: document.getElementById("badge"),
  allowAll: document.getElementById("allow-all"),
  hint: document.getElementById("hint"),
  search: document.getElementById("search"),
  selectAll: document.getElementById("select-all"),
  clearAll: document.getElementById("clear-all"),
  list: document.getElementById("persona-list"),
  empty: document.getElementById("empty"),
  status: document.getElementById("status"),
  save: document.getElementById("save"),
};

const state = {
  personas: [],
  selected: new Set(),
  loaded: false,
  saving: false,
};

function setStatus(text, kind = "") {
  els.status.textContent = text;
  els.status.dataset.kind = kind;
}

function renderPersonas() {
  const keyword = (els.search.value || "").trim().toLowerCase();
  const visible = state.personas.filter((persona) => {
    if (!keyword) return true;
    return (
      persona.id.toLowerCase().includes(keyword) ||
      (persona.name || "").toLowerCase().includes(keyword) ||
      (persona.preview || "").toLowerCase().includes(keyword)
    );
  });

  els.list.replaceChildren();
  for (const persona of visible) {
    const item = document.createElement("li");
    item.className = "persona";

    const label = document.createElement("label");
    label.className = "persona-main";

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.selected.has(persona.id);
    checkbox.dataset.personaId = persona.id;
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.selected.add(persona.id);
      else state.selected.delete(persona.id);
      updateHint();
    });

    const text = document.createElement("span");
    text.className = "persona-text";

    const name = document.createElement("strong");
    name.textContent = persona.id;
    text.appendChild(name);

    if (persona.name && persona.name !== persona.id) {
      const alias = document.createElement("span");
      alias.className = "alias";
      alias.textContent = persona.name;
      text.appendChild(alias);
    }

    if (persona.preview) {
      const preview = document.createElement("small");
      preview.textContent = persona.preview;
      text.appendChild(preview);
    }

    label.append(checkbox, text);
    item.appendChild(label);
    els.list.appendChild(item);
  }

  els.empty.hidden = state.personas.length > 0;
  if (visible.length === 0 && state.personas.length > 0) {
    const none = document.createElement("li");
    none.className = "empty";
    none.textContent = "没有匹配的人格。";
    els.list.appendChild(none);
  }
  updateHint();
}

function updateHint() {
  const total = state.personas.length;
  if (els.allowAll.checked) {
    els.hint.textContent = `已开启「全部人格」：用户可以在 ${total} 个人格里任意切换。`;
    els.list.classList.add("muted");
    return;
  }
  els.list.classList.remove("muted");
  els.hint.textContent = `已选 ${state.selected.size} / ${total} 个人格可被用户切换。`;
}

function setBusy(busy) {
  els.save.disabled = busy;
  els.badge.textContent = busy ? "保存中…" : "已连接";
}

async function load() {
  try {
    await bridge.ready();
    const data = await bridge.apiGet("state");
    state.personas = Array.isArray(data.personas) ? data.personas : [];
    const settings = data.settings || {};
    els.allowAll.checked = Boolean(settings.allow_all_personas);
    state.selected = new Set(
      Array.isArray(settings.allowed_personas) ? settings.allowed_personas : [],
    );
    if (state.selected.size === 0) {
      for (const persona of state.personas) state.selected.add(persona.id);
    }
    state.loaded = true;
    els.badge.textContent = "已连接";
    renderPersonas();
    setStatus("");
  } catch (error) {
    els.badge.textContent = "加载失败";
    setStatus(`读取失败：${error.message || error}`, "error");
  }
}

els.allowAll.addEventListener("change", updateHint);

els.search.addEventListener("input", renderPersonas);

els.selectAll.addEventListener("click", () => {
  for (const persona of state.personas) state.selected.add(persona.id);
  renderPersonas();
});

els.clearAll.addEventListener("click", () => {
  state.selected.clear();
  renderPersonas();
});

els.save.addEventListener("click", async () => {
  if (!state.loaded || state.saving) return;
  state.saving = true;
  setBusy(true);
  setStatus("");
  try {
    const result = await bridge.apiPost("save", {
      allow_all_personas: els.allowAll.checked,
      allowed_personas: [...state.selected],
    });
    const settings = (result && result.settings) || {};
    if (typeof settings.allow_all_personas === "boolean") {
      els.allowAll.checked = settings.allow_all_personas;
    }
    if (Array.isArray(settings.allowed_personas)) {
      state.selected = new Set(settings.allowed_personas);
    }
    renderPersonas();
    setStatus("已保存，用户下一条消息即可使用新人格。", "success");
  } catch (error) {
    setStatus(`保存失败：${error.message || error}`, "error");
  } finally {
    state.saving = false;
    setBusy(false);
  }
});

load();
