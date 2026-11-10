// Injected next to recorder.js: read-only helpers the probe script uses to look at the interface.
// Everything is read from the DOM the app renders (data attributes, classes, glyphs), never from the app's objects,
// and nothing here depends on the interface language.
(() => {
  "use strict";
  const q = (selector) => document.querySelector(selector);
  const all = (selector) => [...document.querySelectorAll(selector)];
  const shown = (selector) => Boolean(q(selector)) && !q(selector).classList.contains("hidden");
  const clock = (text) => {
    const [minutes, seconds] = (text ?? "").trim().split(":").map(Number);
    return Number.isFinite(minutes) && Number.isFinite(seconds) ? minutes * 60 + seconds : 0;
  };

  window.__ui = {
    /** The state the baseline compares after every step. */
    state() {
      const icons = all(".topbar button.icon"); // song menu, undo, redo
      const tab = q(".panel.right .segmented.tabs button.on")?.dataset.value ?? "";
      const overlay = !shown(".overlay") ? "hidden" : q(".overlay .dropzone") ? "drop" : q(".overlay .progress.error") ? "error" : "progress";
      return {
        phase: document.body.dataset.phase ?? "",
        listen: document.body.dataset.listen ?? "",
        save: q(".save-status")?.dataset.state ?? "",
        playing: q(".transport .play")?.textContent === "❚❚",
        title: q(".topbar .song")?.textContent ?? "",
        duration: (q(".transport .time")?.textContent ?? "").split(" / ")[1] ?? "",
        undo: icons[1] ? !icons[1].disabled : false,
        redo: icons[2] ? !icons[2].disabled : false,
        overlay,
        dialog: shown(".modal"),
        menu: shown(".song-menu"),
        songs: all(".song-menu .song-row").length,
        selected: all(".track-row").findIndex((row) => row.classList.contains("selected")),
        muted: all(".track-row").map((row) => row.querySelector(".chip").classList.contains("on")),
        tab,
        shape: tab === "orbit" ? (q(".panel.right .segmented.grid3 button.on")?.dataset.value ?? "") : "",
        room: tab === "space" ? (q(".panel.right .segmented:not(.tabs) button.on")?.dataset.value ?? "") : "",
        sections: all(".tl-block").length,
        events: all(".tl-mark").length,
      };
    },
    button(name) {
      const icons = all(".topbar button.icon");
      const found = {
        menu: icons[0],
        undo: icons[1],
        redo: icons[2],
        import: q(".topbar button.ghost:not(.icon)"),
        export: q(".topbar button.primary"),
      }[name];
      if (!found) throw new Error(`no ${name} button`);
      return found;
    },
    /** Playhead position in seconds, from where the app drew it (finer than the m:ss read-out). */
    seconds() {
      const total = clock((q(".transport .time")?.textContent ?? "").split(" / ")[1]);
      return (parseFloat(q(".tl-head")?.style.left || "0") / 100) * total;
    },
    meters: () => all(".meter .lvl").map((bar) => Number(bar.dataset.db ?? "-90")),
    lane() {
      const r = q(".tl-lane").getBoundingClientRect();
      return { left: r.left, width: r.width, y: r.top + r.height / 2 };
    },
  };
})();
