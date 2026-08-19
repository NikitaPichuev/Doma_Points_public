import { chromium } from "playwright";

const pageUrl = process.argv[2] || "https://app.galxe.com/quest/D3/GCLw6tZ6jC";
const apiName = process.argv[3] || "PrepareParticipate";
const proxyUrl = process.argv[4] || "";

function parseProxy(proxy) {
  if (!proxy) return undefined;
  const url = new URL(proxy);
  return {
    server: `${url.protocol}//${url.hostname}${url.port ? `:${url.port}` : ""}`,
    username: decodeURIComponent(url.username || ""),
    password: decodeURIComponent(url.password || ""),
  };
}

async function main() {
  const launchOptions = { headless: true };
  const parsedProxy = proxyUrl ? parseProxy(proxyUrl) : undefined;
  if (parsedProxy) {
    launchOptions.proxy = parsedProxy;
  }

  const browser = await chromium.launch(launchOptions);
  try {
    const page = await browser.newPage({
      userAgent:
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
        "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    });
    await page.goto(pageUrl, { waitUntil: "networkidle", timeout: 90000 });
    const captcha = await page.evaluate(async (name) => {
      let webpackRequire;
      window.webpackChunk_N_E.push([
        [Math.floor(Math.random() * 1e9)],
        {},
        (req) => {
          webpackRequire = req;
        },
      ]);
      if (!webpackRequire) {
        throw new Error("webpack runtime was not found");
      }
      const captchaModule = webpackRequire(56547);
      if (!captchaModule || typeof captchaModule.H !== "function") {
        throw new Error("Galxe captcha module was not found");
      }
      return await captchaModule.H({ apiName: name, shouldEncrypt: false });
    }, apiName);
    console.log(JSON.stringify({ ok: true, captcha }));
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.log(JSON.stringify({ ok: false, error: String(err && err.stack ? err.stack : err) }));
  process.exit(1);
});
