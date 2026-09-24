const app = document.querySelector<HTMLDivElement>("#app");
if (!app) {
  throw new Error("找不到 #app 挂载点");
}
app.textContent = "Orbit 8D";
