/**
 * RYZM Tweet Importer
 * tweets.js → Supabase posts table
 */
import { createClient } from '@supabase/supabase-js';
import { readFileSync } from 'fs';

const SUPABASE_URL = 'https://vlecflucollbuizfjuto.supabase.co';
const SUPABASE_ANON_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZsZWNmbHVjb2xsYnVpemZqdXRvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzQ3NzU5MjIsImV4cCI6MjA5MDM1MTkyMn0.rGY64wOY2dLpxJaq-5WZtJt0J4liDBobUc-bgO46R4s';
const X_HANDLE = 'Goeun_6121';

const sb = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

// ── Auto-tagging keywords ──
const TAG_RULES = [
  { tag: 'crypto',    re: /\b(bitcoin|btc|eth|ethereum|crypto|altcoin|defi|nft|binance|coinbase|satoshi|halving|blockchain|web3|solana|sol\b|xrp|doge|memecoin|stablecoin|usdt|usdc|tether)\b/i },
  { tag: 'macro',     re: /\b(macro|gdp|inflation|deflation|recession|economic|economy|fiscal|monetary|trade war|tariff|sanctions|geopolit|supply chain|globali[sz]ation|deglobali[sz]ation)\b/i },
  { tag: 'rates',     re: /\b(interest rate|fed fund|fomc|rate cut|rate hike|yield curve|treasury|bond market|tbill|t-bill|10y |2y |10yr|2yr|spread|basis point|bps|pivot|tightening|easing|qe\b|qt\b)\b/i },
  { tag: 'fed',       re: /\b(federal reserve|the fed |powell|fomc|hawkish|dovish|dot plot|jackson hole|beige book)\b/i },
  { tag: 'oil',       re: /\b(crude|oil price|wti|brent|opec|petroleum|energy crisis|nat ?gas|lng|refin)/i },
  { tag: 'gold',      re: /\b(gold|silver|precious metal|xau|bullion|safe haven)\b/i },
  { tag: 'fx',        re: /\b(dollar index|dxy|eur\/usd|usd\/jpy|forex|currency war|yen |yuan |rmb|renminbi)\b/i },
  { tag: 'equities',  re: /\b(s&p|sp500|spx|nasdaq|dow jones|djia|stock market|russell|vix\b|earnings|buyback|ipo\b)\b/i },
  { tag: 'china',     re: /\b(china|chinese|beijing|pboc|xi jinping|ccp|evergrande|alibaba|tencent|hong kong|shanghai)\b/i },
  { tag: 'japan',     re: /\b(japan|boj\b|nikkei|kuroda|ueda|jgb|carry trade)\b/i },
  { tag: 'europe',    re: /\b(europe|ecb\b|lagarde|eurozone|eu\b|germany|france|boe\b|gilt|bund)\b/i },
  { tag: 'politics',  re: /\b(trump|biden|election|congress|senate|white house|democrat|republican|war\b|military|nato|sanction)\b/i },
  { tag: 'psychology',re: /\b(psychology|sentiment|fear|greed|narrative|behavioral|cognitive|bias|contrarian|herd|panic|euphoria|capitulat|conviction|emotion)\b/i },
  { tag: 'history',   re: /\b(history|historical|century|1929|1971|1987|2008|great depression|roman empire|bretton woods|ancient|civilization|empire)\b/i },
  { tag: 'philosophy',re: /\b(philosophy|philosophical|stoic|nietzsche|socrates|plato|aristotle|existential|meaning of|wisdom)\b/i },
  { tag: 'science',   re: /\b(science|scientific|physics|quantum|entropy|evolution|biology|neuroscience|complexity|emergence|chaos theory)\b/i },
  { tag: 'real-estate',re: /\b(real estate|housing|mortgage|rent crisis|property market|homebuyer|foreclosure|mbs)\b/i },
  { tag: 'insurance', re: /\b(insurance|underwrite|lloyds|premium|actuar|reinsur|risk pool)\b/i },
  { tag: 'thread',    re: /(thread|🧵|^1\/)/i },
];

function autoTag(text) {
  const tags = [];
  for (const { tag, re } of TAG_RULES) {
    if (re.test(text)) tags.push(tag);
  }
  if (tags.length === 0) tags.push('general');
  return [...new Set(tags)];
}

// Parse X archive date format: "Mon Mar 30 11:00:00 +0000 2026"
function parseTwitterDate(str) {
  return new Date(str).toISOString();
}

// Clean full_text: remove t.co URLs at end, unescape HTML entities
function cleanText(text) {
  return text
    .replace(/\s*https:\/\/t\.co\/\w+\s*$/g, '')  // trailing t.co
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .trim();
}

async function main() {
  console.log('Reading tweets.js...');

  const tweetsPath = process.argv[2] || '../twitter-2026-03-30-9c32205f18d7b33bd02b06be3936f199ea67d53b598fef9428432551818cff46/data/tweets.js';
  const raw = readFileSync(tweetsPath, 'utf-8');

  // Remove "window.YTD.tweets.part0 = " prefix
  const jsonStr = raw.replace(/^window\.YTD\.tweets\.part\d+\s*=\s*/, '');
  const tweets = JSON.parse(jsonStr);

  console.log(`Parsed ${tweets.length} tweets total`);

  // Transform tweets to posts
  const posts = tweets.map(item => {
    const t = item.tweet;
    const isReply = !!t.in_reply_to_status_id_str;
    const text = cleanText(t.full_text);
    const tags = autoTag(text);
    if (isReply) tags.push('reply');

    return {
      content: text,
      tags: [...new Set(tags)],
      created_at: parseTwitterDate(t.created_at),
      is_public: true,
      source_url: `https://x.com/${X_HANDLE}/status/${t.id_str}`,
      series: null,
      pinned: false,
    };
  });

  // Stats
  const originals = posts.filter(p => !p.tags.includes('reply')).length;
  const replies = posts.filter(p => p.tags.includes('reply')).length;
  const tagCounts = {};
  posts.forEach(p => p.tags.forEach(t => { tagCounts[t] = (tagCounts[t] || 0) + 1; }));

  console.log(`\nStats:`);
  console.log(`  Original tweets: ${originals}`);
  console.log(`  Replies: ${replies}`);
  console.log(`  Total: ${posts.length}`);
  console.log(`\nTag distribution:`);
  Object.entries(tagCounts)
    .sort((a, b) => b[1] - a[1])
    .forEach(([tag, count]) => console.log(`  ${tag}: ${count}`));

  // Batch insert (Supabase limit: ~1000 rows per request)
  const BATCH_SIZE = 500;
  let inserted = 0;
  let errors = 0;

  console.log(`\nInserting into Supabase in batches of ${BATCH_SIZE}...`);

  for (let i = 0; i < posts.length; i += BATCH_SIZE) {
    const batch = posts.slice(i, i + BATCH_SIZE);
    const { data, error } = await sb.from('posts').insert(batch);

    if (error) {
      console.error(`Batch ${Math.floor(i/BATCH_SIZE)+1} error:`, error.message);
      errors += batch.length;
    } else {
      inserted += batch.length;
      const pct = Math.round((i + batch.length) / posts.length * 100);
      console.log(`  ${inserted}/${posts.length} (${pct}%)`);
    }
  }

  console.log(`\nDone! Inserted: ${inserted}, Errors: ${errors}`);
}

main().catch(console.error);
