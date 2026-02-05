// main.js
// Discord.js + OpenAI Responses API example
// Node: use 22.12.0+ (recommended)
// npm packages: discord.js, openai, dotenv, @discordjs/rest, discord-api-types

import fs from "fs";
import path from "path";
import dotenv from "dotenv";
dotenv.config();

import { Client, GatewayIntentBits, Partials, Collection } from "discord.js";
import { REST } from "@discordjs/rest";
import { Routes } from "discord-api-types/v10";
import OpenAI from "openai";

// -------- config & env -----------
const DISCORD_TOKEN = process.env.DISCORD_TOKEN;
const OPENAI_API_KEY = process.env.OPENAI_API_KEY;
const OWNER_ID = process.env.OWNER_ID || null;
const DEFAULT_MODEL = process.env.DEFAULT_MODEL || "gpt-4o-mini";
const CONFIG_PATH = path.resolve("./config.json");

if (!DISCORD_TOKEN || !OPENAI_API_KEY) {
  console.error("DISCORD_TOKEN and OPENAI_API_KEY must be set in .env");
  process.exit(1);
}

// load or create config
let config = { allowedChannels: [] };
try {
  if (fs.existsSync(CONFIG_PATH)) {
    config = JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8"));
  } else {
    fs.writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2));
  }
} catch (err) {
  console.error("Failed reading/writing config.json:", err);
  process.exit(1);
}

// ---------- init clients ----------
const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.MessageContent, // required if reading message content
  ],
  partials: [Partials.Channel],
});

const openai = new OpenAI({ apiKey: OPENAI_API_KEY });

// In-memory conversation store: channelId -> [{role: "user"|"assistant", content: "..."}, ...]
const conversations = new Map();
const MAX_HISTORY = 12;
const USER_COOLDOWN_SECONDS = 4;
const lastUserTs = new Map();

// ---------- slash commands to register ----------
const commands = [
  {
    name: "allow_channel",
    description: "Allow this channel for AI chat (guild admins only)",
  },
  {
    name: "disallow_channel",
    description: "Remove this channel from AI chat (guild admins only)",
  },
  {
    name: "list_ai_channels",
    description: "List currently allowed AI channels",
  },
];

async function registerCommands() {
  try {
    // register globally is possible, but for faster dev, register per-guild
    // You can change to global by using Routes.applicationCommands(clientId)
    const rest = new REST({ version: "10" }).setToken(DISCORD_TOKEN);

    // Try: register as global (may take up to an hour); here we register per-guild if OWNER_ID present
    // If OWNER_ID provided and it's your guild owner id, you can register to that guild for instant availability.
    if (OWNER_ID) {
      // caution: you need a GUILD_ID to register to a guild. We'll skip guild registration here by default.
      // Fallback: register globally
      await rest.put(Routes.applicationCommands(client.user.id), {
        body: commands,
      });
      console.log("Slash commands registered globally.");
    } else {
      await rest.put(Routes.applicationCommands(client.user.id), {
        body: commands,
      });
      console.log("Slash commands registered globally.");
    }
  } catch (err) {
    console.error("Failed to register slash commands:", err);
  }
}

// ---------- helpers ----------
function saveConfig() {
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2));
}

function pushConversation(channelId, role, content) {
  const key = String(channelId);
  let arr = conversations.get(key) || [];
  arr.push({ role, content });
  // trim
  if (arr.length > MAX_HISTORY * 2) arr = arr.slice(-MAX_HISTORY * 2);
  conversations.set(key, arr);
  return arr;
}

function buildPromptFromHistory(channelId, newUserText) {
  const key = String(channelId);
  const history = conversations.get(key) || [];
  const truncated = history.slice(-MAX_HISTORY * 2); // just guard
  const parts = truncated.map((m) => (m.role === "user" ? `User: ${m.content}` : `Assistant: ${m.content}`));
  parts.push(`User: ${newUserText}`);
  return parts.join("\n\n");
}

// ---------- OpenAI call (Responses API) ----------
async function askOpenAI(promptText, model = DEFAULT_MODEL) {
  // Ensure models & usage are validated in OpenAI dashboard (model names change over time)
  const resp = await openai.responses.create({
    model,
    input: promptText,
  });

  // responses SDK often has `output_text` convenience property
  let out = resp.output_text ?? null;
  if (!out) {
    // fallback: try to extract text from resp.output
    try {
      const o = resp.output?.[0]?.content?.[0];
      out = (o?.text ?? o?.content ?? JSON.stringify(resp)).toString();
    } catch {
      out = JSON.stringify(resp);
    }
  }
  return out;
}

// ---------- event handlers ----------
client.once("ready", async () => {
  console.log(`Logged in as ${client.user.tag} (${client.user.id})`);
  // register commands
  await registerCommands();
});

client.on("interactionCreate", async (interaction) => {
  if (!interaction.isChatInputCommand()) return;

  const { commandName } = interaction;
  if (commandName === "allow_channel") {
    // check admin perms
    if (!interaction.memberPermissions?.has("ManageGuild")) {
      await interaction.reply({ content: "You must have Manage Server permission to use this.", ephemeral: true });
      return;
    }
    const cid = interaction.channelId;
    if (!config.allowedChannels.includes(cid)) {
      config.allowedChannels.push(cid);
      saveConfig();
    }
    await interaction.reply({ content: `This channel is now allowed for AI responses.`, ephemeral: true });
  } else if (commandName === "disallow_channel") {
    if (!interaction.memberPermissions?.has("ManageGuild")) {
      await interaction.reply({ content: "You must have Manage Server permission to use this.", ephemeral: true });
      return;
    }
    const cid = interaction.channelId;
    config.allowedChannels = config.allowedChannels.filter((c) => c !== cid);
    conversations.delete(String(cid));
    saveConfig();
    await interaction.reply({ content: `This channel is no longer allowed for AI responses.`, ephemeral: true });
  } else if (commandName === "list_ai_channels") {
    if (!config.allowedChannels.length) {
      await interaction.reply({ content: "No channels configured.", ephemeral: true });
      return;
    }
    const mentions = config.allowedChannels.map((id) => `<#${id}>`).join(", ");
    await interaction.reply({ content: `Allowed channels: ${mentions}`, ephemeral: true });
  }
});

client.on("messageCreate", async (message) => {
  // ignore bots
  if (message.author.bot) return;

  // only respond in allowed channels
  if (!config.allowedChannels.includes(message.channel.id)) return;

  // enforce per-user cooldown
  const lastTs = lastUserTs.get(message.author.id) || 0;
  const now = Date.now() / 1000;
  if (now - lastTs < USER_COOLDOWN_SECONDS) {
    try {
      await message.react("⏳");
    } catch {}
    return;
  }
  lastUserTs.set(message.author.id, now);

  // add to history
  pushConversation(message.channel.id, "user", message.content);

  // build prompt
  const prompt = buildPromptFromHistory(message.channel.id, message.content);

  // typing indicator
  try {
    await message.channel.sendTyping();
  } catch {}

  // call OpenAI
  let reply;
  try {
    reply = await askOpenAI(prompt);
  } catch (err) {
    console.error("OpenAI error:", err);
    await message.reply(`OpenAI error: ${err.message ?? String(err)}`);
    return;
  }

  // store assistant reply
  pushConversation(message.channel.id, "assistant", reply);

  // truncate reply if too long
  if (reply.length > 1950) reply = reply.slice(0, 1950) + "\n\n[truncated]";

  // reply by referencing (keeps context visible)
  await message.reply({ content: reply }).catch((e) => console.error("send error", e));
});

// ---------- start ----------
client.login(DISCORD_TOKEN).catch((err) => {
  console.error("Failed to login:", err);
  process.exit(1);
});
