import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";

const dir = path.join(process.cwd(), "scripts", "ad-images");
await mkdir(dir, { recursive: true });

const bans = [
  "text-free",
  "no readable words",
  "no logos",
  "no glassmorphism",
  "no frosted glass",
  "no purple blue gradient",
  "no neon glow",
  "no floating UI",
  "no cards",
  "no futuristic tech overlay",
  "not stock photo",
  "natural skin texture",
  "8 to 1 max contrast",
].join(", ");

const items = [
  {
    name: "receptionist-1080.jpg",
    width: 1080,
    height: 1080,
    seed: 4101,
    prompt:
      `Photorealistic editorial image for an Irish small business ad: Irish small-business owner answering a phone inside a cozy independent shop or cafe, diary and appointment calendar visible on the counter, warm natural window light, Limerick or Cork street feel through the window, plain colour palette of warm browns creams muted greens and dark navy, one clear focal point, real slightly imperfect setting, not corporate, not glossy, ${bans}`,
  },
  {
    name: "receptionist-1200x628.jpg",
    width: 1200,
    height: 628,
    seed: 4102,
    prompt:
      `Photorealistic editorial wide link-card image for an Irish small business ad: Irish small-business owner answering a phone inside a cozy independent shop or cafe, diary and appointment calendar visible on the counter, warm natural window light, Galway or Cork street feel through the window, plain colour palette warm browns creams muted greens dark navy, one clear focal point, real slightly imperfect setting, not corporate, not glossy, ${bans}`,
  },
  {
    name: "docops-1080.jpg",
    width: 1080,
    height: 1080,
    seed: 4201,
    prompt:
      `Photorealistic editorial image for an Irish cafe restaurant operations ad: Irish cafe or restaurant owner at a counter sorting paperwork, invoices receipts VAT forms, warm natural light, slightly tired real expression, stack of papers being scanned by a phone, Dublin or Limerick neighbourhood cafe feel, plain palette warm browns creams muted greens dark navy, one clear focal point, not polished SaaS imagery, ${bans}`,
  },
  {
    name: "docops-1200x628.jpg",
    width: 1200,
    height: 628,
    seed: 4202,
    prompt:
      `Photorealistic editorial wide link-card image for an Irish cafe restaurant operations ad: Irish cafe or restaurant owner at counter sorting paperwork, invoices receipts VAT forms, warm natural light, slightly tired real expression, stack of papers being scanned by a phone, Cork or Galway neighbourhood restaurant feel, plain palette warm browns creams muted greens dark navy, one clear focal point, not polished SaaS imagery, ${bans}`,
  },
  {
    name: "websites-1080.jpg",
    width: 1080,
    height: 1080,
    seed: 4301,
    prompt:
      `Photorealistic editorial image for an Irish trades business ad: Irish plumber or electrician sitting in a work van checking phone with booking calendar visible as shapes only no readable text, real Irish street or countryside edge visible outside, tools in background, practical used van interior, warm natural light, plain palette warm browns creams muted greens dark navy, one clear focal point, not stock-photo-clean, ${bans}`,
  },
  {
    name: "websites-1200x628.jpg",
    width: 1200,
    height: 628,
    seed: 4302,
    prompt:
      `Photorealistic editorial wide link-card image for an Irish trades business ad: Irish plumber or electrician in work van checking phone with booking calendar visible as shapes only no readable text, real Irish street or landscape outside with Limerick Galway or Cork feel, tools in background, practical used van interior, warm natural light, plain palette warm browns creams muted greens dark navy, one clear focal point, not stock-photo-clean, ${bans}`,
  },
  {
    name: "audit-1080.jpg",
    width: 1080,
    height: 1080,
    seed: 4401,
    prompt:
      `Photorealistic editorial image for an Irish business audit ad: clipboard with handwritten punch list and magnifying glass on counter in front of Irish high-street shopfront, diagnostic practical feel, plain anti-corporate composition, warm natural light, Limerick or Cork high street, plain palette warm browns creams muted greens dark navy, one clear focal point, no harsh black white contrast, ${bans}`,
  },
  {
    name: "audit-1200x628.jpg",
    width: 1200,
    height: 628,
    seed: 4402,
    prompt:
      `Photorealistic editorial wide link-card image for an Irish business audit ad: clipboard with handwritten punch list and magnifying glass on counter with Irish high-street shopfront behind it, diagnostic practical feel, plain anti-corporate composition, warm natural light, Galway Dublin or Limerick high street, plain palette warm browns creams muted greens dark navy, one clear focal point, no harsh black white contrast, ${bans}`,
  },
];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function magicHex(filePath) {
  const buffer = await readFile(filePath).catch(() => Buffer.alloc(0));
  return buffer.subarray(0, 3).toString("hex");
}

async function fetchImage(item, attempt) {
  const seed = item.seed + attempt - 1;
  const encoded = encodeURIComponent(item.prompt);
  const url = `https://image.pollinations.ai/prompt/${encoded}?width=${item.width}&height=${item.height}&seed=${seed}&nologo=true&model=flux&turbo=true`;
  const response = await fetch(url, { signal: AbortSignal.timeout(180_000) });
  const buffer = Buffer.from(await response.arrayBuffer());
  return { response, buffer };
}

await Promise.allSettled([
  rm(path.join(dir, "test-node.jpg"), { force: true }),
  rm(path.join(dir, "test-pollinations.jpg"), { force: true }),
  rm(path.join(dir, "test-pollinations-http.jpg"), { force: true }),
]);

for (const [index, item] of items.entries()) {
  const out = path.join(dir, item.name);
  let ok = false;

  for (let attempt = 1; attempt <= 3; attempt++) {
    console.log(`Fetching ${item.name} attempt ${attempt} (${item.width}x${item.height})...`);
    try {
      const { response, buffer } = await fetchImage(item, attempt);
      await writeFile(out, buffer);

      const magic = await magicHex(out);
      if (response.ok && magic === "ffd8ff") {
        console.log(`  OK JPEG ${buffer.length} bytes -> ${out}`);
        ok = true;
        break;
      }

      const preview = buffer.subarray(0, 120).toString("utf8");
      console.log(`  FAIL status=${response.status} magic=${magic} preview=${preview}`);
      await rm(out, { force: true });
    } catch (error) {
      console.log(`  FAIL ${error.name}: ${error.message}`);
      await rm(out, { force: true });
    }

    if (attempt < 3) {
      console.log("  Waiting 35s before retry...");
      await sleep(35_000);
    }
  }

  if (!ok) {
    throw new Error(`Failed to fetch valid JPEG for ${item.name}`);
  }

  if (index < items.length - 1) {
    console.log("Waiting 35s for Pollinations queue...");
    await sleep(35_000);
  }
}

console.log("Final verification:");
for (const item of items.sort((a, b) => a.name.localeCompare(b.name))) {
  const out = path.join(dir, item.name);
  const buffer = await readFile(out);
  const magic = buffer.subarray(0, 3).toString("hex");
  console.log(`${out} | ${buffer.length} bytes | magic=${magic}`);
}
