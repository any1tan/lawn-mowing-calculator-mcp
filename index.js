#!/usr/bin/env node
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
const SITE = "https://lawnmowingcalculator.com";
const s = new McpServer({ name: "lawn-mowing-calculator", version: "1.0.0" });
const out = (o) => ({ content: [{ type: "text", text: JSON.stringify({ ...o, more: SITE }, null, 2) }] });
s.tool("lawn_area", "Lawn area from length and width in feet (minus excluded sq ft); returns sq ft, sq m, acres.",
  { lengthFt: z.number(), widthFt: z.number(), excludeSqFt: z.number().default(0) },
  async ({ lengthFt, widthFt, excludeSqFt }) => {
    const sqft = Math.max(0, lengthFt * widthFt - excludeSqFt);
    return out({ sqft, sqm: sqft * 0.092903, acres: sqft / 43560 });
  });
s.tool("mowing_time", "Estimated mowing time from area, deck width, speed and overlap (default 10%, 80% efficiency).",
  { sqft: z.number(), deckInches: z.number(), mph: z.number().default(3), overlapPct: z.number().default(10), efficiency: z.number().default(0.8) },
  async ({ sqft, deckInches, mph, overlapPct, efficiency }) => {
    const swathFt = deckInches / 12 * (1 - overlapPct / 100);
    const sqftPerHr = mph * 5280 * swathFt * efficiency;
    return out({ hours: sqft / sqftPerHr, minutes: sqft / sqftPerHr * 60, sqftPerHour: sqftPerHr });
  });
s.tool("mowing_price", "Mowing job price from area: per-acre rate or hourly rate x time, with a minimum charge.",
  { sqft: z.number(), ratePerAcre: z.number().optional(), hourlyRate: z.number().optional(), hours: z.number().optional(), minimumCharge: z.number().default(0) },
  async ({ sqft, ratePerAcre, hourlyRate, hours, minimumCharge }) => {
    const acres = sqft / 43560;
    let price = ratePerAcre != null ? acres * ratePerAcre : hourlyRate != null && hours != null ? hourlyRate * hours : null;
    if (price == null) return out({ error: "Give ratePerAcre, or hourlyRate plus hours." });
    return out({ acres, price: Math.max(price, minimumCharge), minimumApplied: price < minimumCharge });
  });
await s.connect(new StdioServerTransport());
