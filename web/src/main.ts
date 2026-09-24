import "./styles.css";
import { App } from "./app";
import { T } from "./i18n";

document.documentElement.lang = T.locale;
document.documentElement.style.fontFamily = T.fontStack; // 日文界面用日文字体，不借用中文字体显示假名
document.title = T.ui.appName;

const root = document.querySelector<HTMLDivElement>("#app");
if (!root) throw new Error("Mount point #app is missing");
void new App().start(root);
