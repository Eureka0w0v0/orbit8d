import "./styles.css";
import { App } from "./app";

const root = document.querySelector<HTMLDivElement>("#app");
if (!root) throw new Error("找不到 #app 挂载点");
void new App().start(root);
