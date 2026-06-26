#!/usr/bin/env node

const fs = require("fs");
const http = require("http");
const path = require("path");
const { spawn } = require("child_process");

const REPO_ROOT = path.resolve(__dirname, "..");
const MANIFEST_PATH = path.join(REPO_ROOT, "static/gallery-previews/manifest.json");
const VIEWPORT = { width: 1280, height: 920 };
const EXPECTED_VIEWER = { width: 728, height: 626 };

const MIME_TYPES = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".mp4": "video/mp4",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".wasm": "application/wasm",
  ".ico": "image/x-icon"
};

function parseArgs(argv) {
  const options = {
    crf: 20,
    headless: false,
    limit: null,
    only: null,
    from: null,
    keepGoing: false
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--headless") options.headless = true;
    else if (arg === "--keep-going") options.keepGoing = true;
    else if (arg === "--crf") options.crf = Number(argv[++i]);
    else if (arg === "--limit") options.limit = Number(argv[++i]);
    else if (arg === "--only") options.only = argv[++i];
    else if (arg === "--from") options.from = argv[++i];
    else if (arg === "--help" || arg === "-h") {
      console.log([
        "Usage: node scripts/regenerate-gallery-previews.cjs [options]",
        "",
        "Options:",
        "  --only <key>       Regenerate one manifest key, e.g. allegro_v5/apple/4",
        "  --from <key>       Start from a manifest key and continue",
        "  --limit <n>        Regenerate at most n previews",
        "  --crf <n>          x264 CRF value, default 20",
        "  --headless         Use headless Chromium. Headful is much faster on macOS GPU.",
        "  --keep-going       Continue after a failed entry"
      ].join("\n"));
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }

  if (!Number.isFinite(options.crf)) throw new Error("--crf must be a number");
  if (options.limit != null && (!Number.isInteger(options.limit) || options.limit <= 0)) {
    throw new Error("--limit must be a positive integer");
  }
  return options;
}

function loadChromium() {
  try {
    return require("playwright").chromium;
  } catch (error) {
    console.error("Playwright is required. Install it with: npm install -D playwright");
    process.exit(1);
  }
}

function contentType(filePath) {
  return MIME_TYPES[path.extname(filePath).toLowerCase()] || "application/octet-stream";
}

function safeFilePath(urlPath) {
  const cleanPath = decodeURIComponent(urlPath.split("?")[0]).replace(/^\/+/, "");
  const filePath = path.resolve(REPO_ROOT, cleanPath || "index.html");
  if (!filePath.startsWith(REPO_ROOT + path.sep)) return null;
  return filePath;
}

function serveFile(req, res, filePath) {
  fs.stat(filePath, (statError, stat) => {
    if (statError || !stat.isFile()) {
      res.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
      res.end("Not found");
      return;
    }

    const range = req.headers.range;
    const headers = {
      "accept-ranges": "bytes",
      "cache-control": "no-store",
      "content-type": contentType(filePath)
    };

    if (range) {
      const match = range.match(/^bytes=(\d*)-(\d*)$/);
      const start = match?.[1] ? Number(match[1]) : 0;
      const end = match?.[2] ? Number(match[2]) : stat.size - 1;
      if (!match || start >= stat.size || end >= stat.size || start > end) {
        res.writeHead(416, { "content-range": `bytes */${stat.size}` });
        res.end();
        return;
      }
      res.writeHead(206, {
        ...headers,
        "content-length": end - start + 1,
        "content-range": `bytes ${start}-${end}/${stat.size}`
      });
      fs.createReadStream(filePath, { start, end }).pipe(res);
      return;
    }

    res.writeHead(200, { ...headers, "content-length": stat.size });
    fs.createReadStream(filePath).pipe(res);
  });
}

function startStaticServer() {
  const server = http.createServer((req, res) => {
    const filePath = safeFilePath(req.url || "/");
    if (!filePath) {
      res.writeHead(403, { "content-type": "text/plain; charset=utf-8" });
      res.end("Forbidden");
      return;
    }
    serveFile(req, res, filePath);
  });

  return new Promise((resolve, reject) => {
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      resolve({ server, baseUrl: `http://127.0.0.1:${port}` });
    });
  });
}

function bufferFromDataUrl(dataUrl) {
  return Buffer.from(dataUrl.replace(/^data:image\/png;base64,/, ""), "base64");
}

function writeStream(stream, chunk) {
  return new Promise((resolve, reject) => {
    const onError = error => {
      stream.off("drain", onDrain);
      reject(error);
    };
    const onDrain = () => {
      stream.off("error", onError);
      resolve();
    };
    stream.once("error", onError);
    if (stream.write(chunk)) {
      stream.off("error", onError);
      resolve();
    } else {
      stream.once("drain", onDrain);
    }
  });
}

function waitForExit(child) {
  return new Promise((resolve, reject) => {
    let stderr = "";
    child.stderr.on("data", data => {
      stderr += data;
    });
    child.on("error", reject);
    child.on("close", code => {
      if (code === 0) resolve();
      else reject(new Error(`ffmpeg exited ${code}\n${stderr}`));
    });
  });
}

async function stopInstantPreview(page) {
  await page.evaluate(() => {
    document.querySelectorAll(".viewer-preview video").forEach(video => {
      video.pause();
      video.removeAttribute("src");
      video.load();
    });
    document.querySelector(".viewer-preview")?.remove();
  });
}

async function captureDataUrl(page, time, width, height) {
  return page.evaluate(({ time, width, height }) => {
    window.__hrdexdbPreviewCapture.setSourceTime(time);
    const source = document.querySelector("#viewer canvas");
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    canvas.getContext("2d").drawImage(source, 0, 0, width, height);
    return canvas.toDataURL("image/png");
  }, { time, width, height });
}

async function loadEntry(page, baseUrl, key) {
  const [embodiment, objectId, episode] = key.split("/");
  await page.goto(`${baseUrl}/gallery.html#gallery`, {
    waitUntil: "domcontentloaded",
    timeout: 60000
  });
  await page.waitForFunction(() => typeof window.openDetail === "function", null, { timeout: 60000 });
  await page.waitForSelector(`.grid-item[data-object="${objectId}"]`, { state: "attached", timeout: 60000 });
  await page.evaluate(({ objectId, embodiment, episode }) => {
    const numericEpisode = Number(episode);
    window.openDetail(
      objectId,
      embodiment,
      Number.isFinite(numericEpisode) ? numericEpisode : episode
    );
  }, { objectId, embodiment, episode });
  await page.waitForSelector(".episode-card", { timeout: 60000 });
  await page.click(`.episode-card[data-episode="${episode}"]`);
  await page.waitForSelector(`.episode-card.active[data-episode="${episode}"]`, { timeout: 60000 });
  await page.waitForSelector("#load-glb-button:not(:disabled)", { timeout: 60000 });
  await stopInstantPreview(page);
  await page.click("#load-glb-button");
  await page.waitForFunction(() => window.__hrdexdbPreviewCapture?.state?.().loaded, null, {
    timeout: 120000
  });
  return page.evaluate(() => ({
    box: window.__hrdexdbPreviewCapture.viewerBox(),
    state: window.__hrdexdbPreviewCapture.state()
  }));
}

async function renderEntry(page, baseUrl, manifest, key, asset, index, total, completedFrames, totalFrames, runStartedAt, options) {
  const started = Date.now();
  const videoPath = path.join(REPO_ROOT, asset.video);
  const posterPath = path.join(REPO_ROOT, asset.poster);
  const tmpVideoPath = `${videoPath}.tmp.mp4`;
  const tmpPosterPath = `${posterPath}.tmp.png`;
  const fps = asset.fps || 24;
  const frames = asset.frames;
  const start = asset.trim?.start ?? 0;
  const end = asset.trim?.end ?? start + frames / fps;

  fs.mkdirSync(path.dirname(videoPath), { recursive: true });
  fs.rmSync(tmpVideoPath, { force: true });
  fs.rmSync(tmpPosterPath, { force: true });

  const loaded = await loadEntry(page, baseUrl, key);
  const { width, height } = loaded.box;
  if (width !== EXPECTED_VIEWER.width || height !== EXPECTED_VIEWER.height) {
    throw new Error(`${key}: unexpected viewer size ${width}x${height}`);
  }

  const stillCutFraction = manifest.stillCutFraction ?? 0.9;
  const posterTime = start + (end - start) * stillCutFraction;
  fs.writeFileSync(tmpPosterPath, bufferFromDataUrl(await captureDataUrl(page, posterTime, width, height)));

  const ffmpeg = spawn("ffmpeg", [
    "-y",
    "-hide_banner",
    "-loglevel",
    "error",
    "-f",
    "image2pipe",
    "-framerate",
    String(fps),
    "-i",
    "pipe:0",
    "-an",
    "-c:v",
    "libx264",
    "-preset",
    "veryfast",
    "-crf",
    String(options.crf),
    "-pix_fmt",
    "yuv420p",
    "-movflags",
    "+faststart",
    tmpVideoPath
  ]);
  const ffmpegDone = waitForExit(ffmpeg);

  for (let frame = 0; frame < frames; frame += 1) {
    const time = Math.min(end, start + frame / fps);
    await writeStream(ffmpeg.stdin, bufferFromDataUrl(await captureDataUrl(page, time, width, height)));
  }
  ffmpeg.stdin.end();
  await ffmpegDone;

  fs.renameSync(tmpVideoPath, videoPath);
  fs.renameSync(tmpPosterPath, posterPath);
  asset.generatedAt = runStartedAt;

  const elapsed = (Date.now() - started) / 1000;
  const doneFrames = completedFrames + frames;
  const pct = totalFrames ? `, ${((doneFrames / totalFrames) * 100).toFixed(1)}% frames` : "";
  console.log(`[${index + 1}/${total}] ${key}: ${frames}f, ${elapsed.toFixed(1)}s${pct}`);
  return frames;
}

function selectEntries(manifest, options) {
  let entries = Object.entries(manifest.assets);
  if (options.only) entries = entries.filter(([key]) => key === options.only);
  if (options.from) {
    const startIndex = entries.findIndex(([key]) => key === options.from);
    if (startIndex === -1) throw new Error(`--from key not found: ${options.from}`);
    entries = entries.slice(startIndex);
  }
  if (options.limit != null) entries = entries.slice(0, options.limit);
  if (!entries.length) throw new Error("No preview entries selected");
  return entries;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const chromium = loadChromium();
  const manifest = JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf8"));
  const entries = selectEntries(manifest, options);
  const totalFrames = entries.reduce((sum, [, asset]) => sum + (asset.frames || 0), 0);
  const runStartedAt = new Date().toISOString();
  const { server, baseUrl } = await startStaticServer();

  console.log(`Serving ${REPO_ROOT} at ${baseUrl}`);
  console.log(`Regenerating ${entries.length} previews, ${totalFrames} frames, generatedAt=${runStartedAt}`);

  const browser = await chromium.launch({
    headless: options.headless,
    args: ["--window-position=-2000,-2000", `--window-size=${VIEWPORT.width},${VIEWPORT.height}`, "--ignore-gpu-blocklist"]
  });

  let completedFrames = 0;
  const failures = [];

  try {
    const page = await browser.newPage({ viewport: VIEWPORT, deviceScaleFactor: 1 });
    page.setDefaultTimeout(120000);
    page.on("pageerror", error => console.error(`[pageerror] ${error.message}`));
    page.on("requestfailed", request => {
      const url = request.url();
      if (!url.includes("favicon") && !url.includes("willi19.github.io/autodex-gallery/objects/")) {
        console.error(`[requestfailed] ${url} ${request.failure()?.errorText}`);
      }
    });

    for (let index = 0; index < entries.length; index += 1) {
      const [key, asset] = entries[index];
      try {
        completedFrames += await renderEntry(
          page,
          baseUrl,
          manifest,
          key,
          asset,
          index,
          entries.length,
          completedFrames,
          totalFrames,
          runStartedAt,
          options
        );
      } catch (error) {
        failures.push({ key, error });
        console.error(`[failed] ${key}: ${error.message}`);
        await page.goto("about:blank").catch(() => {});
        if (!options.keepGoing) throw error;
      }
    }
  } finally {
    await browser.close().catch(() => {});
    await new Promise(resolve => server.close(resolve));
  }

  manifest.generatedAt = runStartedAt;
  fs.writeFileSync(MANIFEST_PATH, `${JSON.stringify(manifest, null, 2)}\n`);

  if (failures.length) {
    console.error(`Completed with ${failures.length} failures.`);
    process.exitCode = 1;
  } else {
    console.log("Done.");
  }
}

main().catch(error => {
  console.error(error);
  process.exit(1);
});
