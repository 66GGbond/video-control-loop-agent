/* Persisted VCL projects. */
window.ProjectHistory = (() => {
  const grid = document.querySelector("#history-projects");
  const status = document.querySelector("#history-status");
  const newProject = document.querySelector("#history-new-project");
  const deleteDialog = document.querySelector("#delete-project-dialog");
  const deleteName = document.querySelector("#delete-project-name");
  const deleteError = document.querySelector("#delete-project-error");
  const deleteConfirm = document.querySelector("#confirm-delete-project");
  const deleteCancel = document.querySelector("#cancel-delete-project");
  let projects = [];
  let poll = null;
  let loading = false;
  let editing = false;
  let opening = false;
  let pendingDelete = null;
  let deleting = false;
  let mutationRevision = 0;

  const icon = name => `<svg class="ui-icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const message = text => { status.textContent = I18n.text(text); };

  function applyMutation(nextProjects) {
    mutationRevision += 1;
    projects = nextProjects;
    window.dispatchEvent(new CustomEvent("projectnameschange", {detail: projects}));
    renderCards();
  }

  function renderCards() {
    if (editing) return;
    grid.replaceChildren();
    for (const project of projects) {
      const card = document.createElement("article");
      card.className = "history-card";
      const open = document.createElement("button");
      open.type = "button";
      open.className = "history-open";
      open.setAttribute("aria-label", I18n.text("Continue editing") + " · " + project.title);
      const preview = document.createElement("div");
      preview.className = "history-preview";
      if (project.preview_kind) {
        const image = document.createElement("img");
        image.src = project.preview_url;
        image.alt = "";
        image.loading = "lazy";
        image.addEventListener("error", () => {
          image.remove();
          preview.insertAdjacentHTML("afterbegin", icon("folder"));
        }, {once: true});
        preview.append(image);
      } else {
        preview.innerHTML = icon("folder");
      }
      if (project.preview_kind === "video") {
        const badge = document.createElement("span");
        badge.className = "history-video-badge";
        badge.innerHTML = icon("video");
        preview.append(badge);
      }
      const heading = document.createElement("div");
      heading.className = "history-card-heading";
      const title = document.createElement("h2");
      title.textContent = project.title;
      title.translate = false;
      const editLabel = document.createElement("span");
      editLabel.textContent = "Continue editing";
      heading.append(title, editLabel);
      open.append(preview, heading);
      open.addEventListener("click", () => openProject(project, open));

      const footer = document.createElement("div");
      footer.className = "history-card-footer";
      const date = document.createElement("time");
      date.dateTime = project.updated_at;
      date.textContent = new Intl.DateTimeFormat(I18n.language === "zh" ? "zh-CN" : "en",
        {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}).format(new Date(project.updated_at));
      date.translate = false;
      const rename = historyAction("Rename", "pencil", project);
      const remove = historyAction("Delete", "trash", project);
      remove.classList.add("history-delete");
      rename.addEventListener("click", () => renameProject(project, card, heading));
      remove.addEventListener("click", () => {
        if (editing || deleting || opening) return;
        pendingDelete = project;
        deleteName.textContent = project.title;
        deleteError.hidden = true;
        deleteDialog.showModal();
      });
      footer.append(date, rename, remove);
      card.append(open, footer);
      grid.append(card);
    }
    message(projects.length ? "" : "No saved projects yet");
  }

  function historyAction(label, iconName, project) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "history-action";
    button.innerHTML = icon(iconName);
    const text = document.createElement("span");
    text.textContent = label;
    button.append(text);
    button.setAttribute("aria-label", I18n.text(label + " project") + " · " + project.title);
    return button;
  }

  function renameProject(project, card, heading) {
    if (editing || deleting || opening) return;
    editing = true;
    const form = document.createElement("form");
    form.className = "history-rename-form";
    const input = document.createElement("input");
    input.type = "text";
    input.value = project.title;
    input.maxLength = 80;
    input.required = true;
    input.setAttribute("aria-label", "Project name");
    const save = document.createElement("button");
    save.type = "submit";
    save.className = "secondary";
    save.textContent = "Save";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "secondary";
    cancel.textContent = "Cancel";
    form.append(input, save, cancel);
    heading.hidden = true;
    card.querySelector(".history-card-footer").before(form);
    input.focus();
    input.select();
    let saving = false;
    function cancelEdit() {
      if (saving) return;
      editing = false;
      renderCards();
    }
    cancel.addEventListener("click", cancelEdit);
    input.addEventListener("keydown", event => { if (event.key === "Escape") cancelEdit(); });
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const title = input.value.trim();
      if (saving || !title) return;
      saving = input.disabled = save.disabled = cancel.disabled = true;
      try {
        const data = await endpoint("/api/projects/rename", {
          method: "POST", body: JSON.stringify({project_id: project.id, title}),
        });
        editing = false;
        applyMutation(data.projects);
      } catch (error) {
        message(error.message);
        saving = input.disabled = save.disabled = cancel.disabled = false;
        input.focus();
      }
    });
  }

  deleteCancel.addEventListener("click", () => deleteDialog.close());
  deleteDialog.addEventListener("cancel", event => { if (deleting) event.preventDefault(); });
  deleteDialog.addEventListener("close", () => { pendingDelete = null; });
  document.querySelector("#delete-project-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!pendingDelete || deleting) return;
    deleting = deleteConfirm.disabled = deleteCancel.disabled = true;
    deleteError.hidden = true;
    try {
      const data = await endpoint("/api/projects/delete", {
        method: "POST", body: JSON.stringify({project_id: pendingDelete.id}),
      });
      if (data.active_project_id !== window.VCLBridge.state()?.project_id) {
        location.replace("/?new=" + Date.now() + "#history");
        return;
      }
      deleteDialog.close();
      applyMutation(data.projects);
      message(data.projects.length ? "Project deleted." : "No saved projects yet");
      newProject.focus();
    } catch (error) {
      deleteError.textContent = I18n.text(error.message);
      deleteError.hidden = false;
    } finally {
      deleting = deleteConfirm.disabled = deleteCancel.disabled = false;
    }
  });

  async function openProject(project, button) {
    if (editing || deleting || opening) return;
    opening = button.disabled = true;
    clearTimeout(poll);
    poll = null;
    try {
      const data = await endpoint("/api/projects/open", {
        method: "POST", body: JSON.stringify({project_id: project.id}),
      });
      location.assign("/?project=" + encodeURIComponent(data.project_id) + "&opened=" + Date.now() + "#" + data.workspace);
    } catch (error) {
      message(error.message);
      opening = button.disabled = false;
    }
  }

  async function load() {
    if (loading) return;
    loading = true;
    clearTimeout(poll);
    poll = null;
    if (!projects.length) message("Loading project history…");
    grid.setAttribute("aria-busy", "true");
    const revision = mutationRevision;
    try {
      let data = await endpoint("/api/projects");
      if (revision !== mutationRevision) return;
      projects = data.projects;
      renderCards();
      if (projects.some(project => project.title_source === "pending")) {
        data = await endpoint("/api/project-titles", {
          method: "POST", body: JSON.stringify({language: I18n.language}),
        });
        if (revision !== mutationRevision) return;
        projects = data.projects;
        renderCards();
      }
    } catch (error) {
      if (revision === mutationRevision) {
        message("Project history could not load. Retrying…");
        if (document.body.dataset.workspace === "history") poll = setTimeout(load, 3000);
      }
    } finally {
      loading = false;
      grid.setAttribute("aria-busy", "false");
      if (!poll && document.body.dataset.workspace === "history" &&
          projects.some(project => ["pending", "naming"].includes(project.title_source))) {
        poll = setTimeout(load, 1500);
      }
    }
  }

  newProject.addEventListener("click", () => window.openNewProject());
  window.addEventListener("languagechange", renderCards);
  return {load};
})();
