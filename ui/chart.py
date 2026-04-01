"""Generate the conversation-chart HTML used by both the menu-bar popup and test harness."""

from __future__ import annotations

import json


def generate_chart_html(
    title: str | None,
    breakdown: dict,
    turns: list[dict],
    active_optimizers: dict[str, int] | None = None,
) -> str:
    """Return a self-contained HTML page with a stacked-bar + cumulative-line chart.

    Parameters
    ----------
    title:
        Optional heading shown above the legend.  When *None* the extra
        vertical space is reclaimed by the chart.
    breakdown:
        ``{"system": int, "tools": int, ...}`` — overhead char counts.
        May also contain ``system_blocks``, ``tool_groups``, ``tools_count``.
    turns:
        Per-turn dicts with keys such as ``index``, ``label``, ``user_text``,
        ``tool_result``, ``assistant_text``, ``tool_call``, ``image``,
        ``images``, and optional ``blocks``.
    active_optimizers:
        ``{chart_key: keep_turns}`` for each enabled optimizer.  Only
        keys present here will be hatched and subtracted from the
        cumulative optimized line.  ``None`` means no optimizers.
    """
    if active_optimizers is None:
        active_optimizers = {}
    breakdown_json = json.dumps(breakdown)
    turns_json = json.dumps(turns)
    active_opt_json = json.dumps(active_optimizers)

    chart_height = "calc(100vh - 100px)" if title else "calc(100vh - 80px)"
    title_div = f'<div class="title">{title}</div>' if title else ""

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', sans-serif;
            background: #000; color: #f5f5f7; padding: 8px;
            -webkit-font-smoothing: antialiased;
        }}
        .title {{ font-size: 14px; font-weight: 600; margin-bottom: 10px; color: #f5f5f7; }}
        .chart-container {{ position: relative; width: 100%; height: calc(70vh - 60px); }}
        .cache-container {{ position: relative; width: 100%; height: calc(30vh - 40px); margin-top: 4px; }}
        canvas {{ width: 100% !important; height: 100% !important; }}
        .legend {{
            display: flex; flex-wrap: wrap; gap: 16px; margin-bottom: 14px; font-size: 12px;
        }}
        .legend-item {{ display: flex; align-items: center; gap: 6px; font-weight: 500; }}
        .legend-dot {{ width: 10px; height: 10px; border-radius: 2px; }}
    </style>
    </head>
    <body>
        {title_div}
        <div class="legend">
            <div class="legend-item">
                <div class="legend-dot" style="background:#86868b;"></div>
                <span style="color:#c9c9ce;">System prompt</span>
            </div>
            <div class="legend-item">
                <div class="legend-dot" style="background:#a1a1a6;"></div>
                <span style="color:#d1d1d6;">Tools</span>
            </div>
            <div class="legend-item">
                <div class="legend-dot" style="background:#2997ff;"></div>
                <span style="color:#2997ff;">User text</span>
            </div>
            <div class="legend-item">
                <div class="legend-dot" style="background:#30d158;"></div>
                <span style="color:#30d158;">Tool results</span>
            </div>
            <div class="legend-item">
                <div class="legend-dot" style="background:#ff375f;"></div>
                <span style="color:#ff375f;">Images</span>
            </div>
            <div class="legend-item">
                <div class="legend-dot" style="background:#ff9f0a;"></div>
                <span style="color:#ff9f0a;">Assistant output</span>
            </div>
            <div class="legend-item">
                <div class="legend-dot" style="background:transparent; border:1.5px solid #f5f5f7; border-radius:50%;"></div>
                <span style="color:#f5f5f7;">Cumulative (full)</span>
            </div>
            <div class="legend-item">
                <div class="legend-dot" style="background:transparent; border:1.5px solid #5e5ce6; border-radius:50%;"></div>
                <span style="color:#5e5ce6;">Cumulative (optimized)</span>
            </div>
            <div class="legend-item">
                <div class="legend-dot" style="background:transparent; border:1.5px solid #ff3b30; border-radius:50%;"></div>
                <span style="color:#ff3b30;">API context ×3.5</span>
            </div>
            <div class="legend-item">
                <div class="legend-dot" style="background:transparent; border:1.5px solid #30d158; border-radius:50%;"></div>
                <span style="color:#30d158;">Cache read ×3.5</span>
            </div>
            <div class="legend-item">
                <div class="legend-dot" style="background:#5e5ce6;"></div>
                <span style="color:#5e5ce6;">Cache: ephemeral</span>
            </div>
            <div class="legend-item">
                <div class="legend-dot" style="background:#f59e0b;"></div>
                <span style="color:#f59e0b;">Cache: custom</span>
            </div>
        </div>
        <div class="chart-container">
            <canvas id="chart"></canvas>
        </div>
        <div class="cache-container">
            <canvas id="cache-chart"></canvas>
        </div>
        <div id="tooltip" style="
            display:none; position:fixed; pointer-events:none; z-index:100;
            background:rgba(30,30,30,0.95); border:1px solid rgba(255,255,255,0.15);
            border-radius:8px; padding:10px; font-size:11px; color:#f5f5f7;
            backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px);
            max-width:320px; box-shadow:0 8px 32px rgba(0,0,0,0.5);
        "></div>
        <script>
        const bd = {breakdown_json};
        const turns = {turns_json};
        const canvas = document.getElementById('chart');
        const ctx = canvas.getContext('2d');
        const tooltip = document.getElementById('tooltip');

        // Apple-style palette
        const C_SYSTEM   = '#86868b';
        const C_TOOLS    = '#a1a1a6';
        const C_USERTEXT = '#2997ff';
        const C_TOOLRES  = '#30d158';
        const C_IMAGE    = '#ff375f';
        const C_ASSIST   = '#ff9f0a';
        const C_CUM      = '#f5f5f7';
        const C_CUM_OPT  = '#5e5ce6';
        const C_API      = '#ff3b30';
        const C_CACHE    = '#30d158';
        const C_GRID     = 'rgba(255,255,255,0.06)';
        const C_LABEL    = '#86868b';
        const ACTIVE_OPT = {active_opt_json};

        // Cache control type colors
        const CACHE_COLORS = {{
            'ephemeral': '#5e5ce6',
            'custom': '#f59e0b'
        }};

        // Generate distinct hues for individual images within the red family
        function imageColor(i, total) {{
            if (total <= 1) return C_IMAGE;
            const hues = [348, 0, 15, 330, 20];
            const h = hues[i % hues.length];
            const l = 55 + (i % 3) * 8;
            return `hsl(${{h}}, 85%, ${{l}}%)`;
        }}

        function fmtBytes(b) {{
            if (b >= 1048576) return (b/1048576).toFixed(1) + ' MB';
            if (b >= 1024) return (b/1024).toFixed(1) + ' KB';
            return b + ' B';
        }}

        function fmtChars(v) {{
            if (v >= 1e6) return (v/1e6).toFixed(1) + 'M';
            if (v >= 1e3) return (v/1e3).toFixed(1) + 'k';
            return v.toString();
        }}

        // Store hit regions for hover detection
        let hitRegions = [];

        function esc(s) {{ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }}

        function filterBlocks(turnIdx, ...types) {{
            const t = turns[turnIdx];
            if (!t || !t.blocks) return [];
            return t.blocks.filter(b => types.includes(b.type));
        }}

        function blockList(blocks, max) {{
            const items = blocks.slice(0, max || 6);
            let html = items.map(b => {{
                const s = esc(b.summary).substring(0, 80);
                return `<div style="color:#ccc; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:280px;">${{s}}</div>`;
            }}).join('');
            if (blocks.length > (max || 6)) html += `<div style="color:#666;">+${{blocks.length - (max||6)}} more</div>`;
            return html;
        }}

        function buildTooltip(hit) {{
            const row = (label, val) => `<span style="color:#86868b;">${{label}}</span><span>${{val}}</span>`;
            const grid = (rows) => `<div style="display:grid; grid-template-columns:auto 1fr; gap:2px 10px;">${{rows}}</div>`;

            // Overhead column
            if (hit.turnIndex < 0) {{
                if (hit.type === 'system') {{
                    let rows = row('System prompt', fmtChars(hit.value) + ' chars');
                    if (bd.system_blocks) {{
                        for (const sb of bd.system_blocks) {{
                            rows += row('', esc(sb.preview).substring(0, 50) + '... (' + fmtChars(sb.chars) + ')');
                        }}
                    }}
                    return grid(rows);
                }}
                if (hit.type === 'tools') {{
                    let rows = row('Tools', (bd.tools_count || 0) + ' tools, ' + fmtChars(hit.value) + ' chars');
                    if (bd.tool_groups) {{
                        for (const g of bd.tool_groups) {{
                            rows += row(esc(g.prefix), g.count + ' tools, ' + fmtChars(g.chars));
                        }}
                    }}
                    return grid(rows);
                }}
            }}

            const ti = hit.turnIndex;
            const t = turns[ti];
            const turnLabel = t ? ('#' + t.index + ' ' + esc(t.label || '')) : '';

            if (hit.type === 'image' && hit.info) {{
                const info = hit.info;
                const dims = (info.width && info.height) ? `${{info.width}} &times; ${{info.height}} px` : '?';
                let thumbHtml = '';
                if (info.thumbnail) {{
                    thumbHtml = `<img src="data:image/jpeg;base64,${{info.thumbnail}}"
                        style="max-width:200px; max-height:120px; border-radius:4px;
                        margin-bottom:8px; display:block; background:#1a1a1a;">`;
                }}
                let rows = row('Turn', turnLabel);
                rows += row('Dimensions', dims);
                rows += row('Format', info.media_type || '?');
                rows += row('Raw size', fmtBytes(info.raw_bytes));
                rows += row('Base64', fmtChars(info.base64_chars) + ' chars');
                if (hit.stripped) rows += row('Status', '<span style="color:#5e5ce6;">stripped by optimizer</span>');
                return thumbHtml + grid(rows);
            }}

            // Unified turn breakdown tooltip
            if (t) {{
                const trBlocks = filterBlocks(ti, 'tool_result');
                const tcBlocks = filterBlocks(ti, 'tool_call', 'mcp_tool_call', 'server_tool_call');
                const txtBlocks = filterBlocks(ti, 'assistant_text', 'thinking');

                let rows = row('Turn', turnLabel);

                // Breakdown table
                const parts = [];
                if (t.user_text) parts.push(row('<span style="color:' + C_USERTEXT + ';">&block;</span> User text', fmtChars(t.user_text) + ' chars'));
                if (t.tool_result) {{
                    let trLabel = fmtChars(t.tool_result) + ' chars';
                    if (trBlocks.length) trLabel += ', ' + trBlocks.length + ' result(s)';
                    parts.push(row('<span style="color:' + C_TOOLRES + ';">&block;</span> Tool results', trLabel));
                }}
                if (t.bash) parts.push(row('&emsp;Bash', fmtChars(t.bash) + ' chars'));
                if (t.stale_read) parts.push(row('&emsp;Stale reads', fmtChars(t.stale_read) + ' chars'));
                const assistChars = (t.assistant_text || 0) + (t.tool_call || 0);
                if (assistChars) {{
                    let aLabel = fmtChars(assistChars) + ' chars';
                    if (tcBlocks.length) aLabel += ', ' + tcBlocks.length + ' tool call(s)';
                    parts.push(row('<span style="color:' + C_ASSIST + ';">&block;</span> Assistant', aLabel));
                }}
                if (t.thinking) parts.push(row('&emsp;Thinking', fmtChars(t.thinking) + ' chars'));
                if (t.image) parts.push(row('<span style="color:' + C_IMAGE + ';">&block;</span> Images', fmtChars(t.image) + ' chars'));

                rows += parts.join('');

                // Stripped status
                const strippedTool = t.stripped_tool || 0;
                const strippedThink = t.stripped_thinking || 0;
                if (strippedTool + strippedThink > 0) {{
                    let sLabel = '<span style="color:#5e5ce6;">stripped ' + fmtChars(strippedTool + strippedThink) + ' chars</span>';
                    rows += row('Optimizer', sLabel);
                }}

                // Block list
                const allBlocks = [...trBlocks, ...txtBlocks, ...tcBlocks];
                return grid(rows) + (allBlocks.length ? '<div style="margin-top:6px; border-top:1px solid #333; padding-top:6px;">' + blockList(allBlocks, 8) + '</div>' : '');
            }}

            if (hit.type === 'api_usage') {{
                const ti = hit.turnIndex;
                const t = turns[ti];
                const turnLabel = t ? ('#' + t.index + ' ' + esc(t.label || '')) : '';
                const inputTok = t ? (t.input_tokens || 0) : 0;
                const outputTok = t ? (t.output_tokens || 0) : 0;
                const cacheRead = t ? (t.cache_read_input_tokens || 0) : 0;
                const cacheCreate = t ? (t.cache_creation_input_tokens || 0) : 0;
                const totalTok = inputTok + cacheRead + cacheCreate;
                let rows = row('Turn', turnLabel);
                rows += row('Input tokens', inputTok.toLocaleString());
                rows += row('Cache read tokens', cacheRead.toLocaleString());
                rows += row('Cache create tokens', cacheCreate.toLocaleString());
                const cachePct = totalTok > 0 ? Math.round(cacheRead * 100 / totalTok) : 0;
                rows += row('Total context tokens', totalTok.toLocaleString());
                rows += row('Cache hit', cachePct + '%');
                rows += row('Output tokens', outputTok.toLocaleString());
                return grid(rows);
            }}

            // Fallback
            return grid(row('Type', hit.type) + row('Size', fmtChars(hit.value) + ' chars'));
        }}

        function draw() {{
            const dpr = window.devicePixelRatio || 1;
            const rect = canvas.getBoundingClientRect();
            canvas.width = rect.width * dpr;
            canvas.height = rect.height * dpr;
            ctx.scale(dpr, dpr);
            const W = rect.width, H = rect.height;

            ctx.clearRect(0, 0, W, H);
            hitRegions = [];

            if (turns.length === 0) {{
                ctx.fillStyle = C_LABEL;
                ctx.font = '14px -apple-system, sans-serif';
                ctx.textAlign = 'center';
                ctx.fillText('No turn data yet', W / 2, H / 2);
                return;
            }}

            const pad = {{ top: 20, right: 60, bottom: 50, left: 60 }};
            const cW = W - pad.left - pad.right;
            const cH = H - pad.top - pad.bottom;

            const nCols = 1 + turns.length;
            const gap = cW / nCols;
            const barW = Math.max(4, Math.min(40, gap * 0.7));

            const columns = [];
            const labels = [];

            // Column 0: overhead
            columns.push([
                {{ color: C_SYSTEM, value: bd.system, type: 'system' }},
                {{ color: C_TOOLS, value: bd.tools, type: 'tools' }},
            ]);
            labels.push('overhead');

            for (let ti = 0; ti < turns.length; ti++) {{
                const t = turns[ti];
                const imgs = t.images || [];
                const staleRead = t.stale_read || 0;
                const bashChars = t.bash || 0;
                const freshToolRes = Math.max(0, t.tool_result - staleRead - bashChars);
                const segs = [
                    {{ color: C_USERTEXT, value: t.user_text, type: 'user_text' }},
                    {{ color: C_TOOLRES, value: freshToolRes, type: 'tool_result' }},
                ];
                if (bashChars > 0) {{
                    segs.push({{ color: C_TOOLRES, value: bashChars, type: 'bash' }});
                }}
                if (staleRead > 0) {{
                    segs.push({{ color: C_TOOLRES, value: staleRead, type: 'stale_read' }});
                }}

                // Split image portion into individual image segments
                if (imgs.length > 0) {{
                    for (let ii = 0; ii < imgs.length; ii++) {{
                        segs.push({{
                            color: imageColor(ii, imgs.length),
                            value: imgs[ii].token_chars,
                            type: 'image',
                            imageInfo: imgs[ii],
                            imageIndex: ii,
                            turnIndex: ti,
                        }});
                    }}
                }}

                segs.push({{ color: C_ASSIST, value: t.assistant_text + t.tool_call, type: 'assistant' }});
                const thinkChars = t.thinking || 0;
                if (thinkChars > 0) {{
                    segs.push({{ color: C_ASSIST, value: thinkChars, type: 'thinking' }});
                }}
                columns.push(segs);
                labels.push('#' + t.index);
            }}

            const colTotals = columns.map(segs => segs.reduce((s, seg) => s + seg.value, 0));
            let maxVol = Math.max(...colTotals, 1);

            // Cumulative lines: full and optimized
            const hasAnyOpt = Object.keys(ACTIVE_OPT).length > 0;
            const overhead = (bd.system || 0) + (bd.tools || 0);
            let cumFullValues = [];
            let cumOptValues = [];
            let cumFullSum = 0;
            let cumOptSum = 0;
            for (let i = 0; i < turns.length; i++) {{
                cumFullSum += colTotals[i + 1];
                const stripped = Math.min(colTotals[i + 1], (turns[i].stripped_tool || 0) + (turns[i].stripped_thinking || 0));
                cumOptSum += colTotals[i + 1] - stripped;
                cumFullValues.push(cumFullSum + overhead);
                cumOptValues.push(cumOptSum + overhead);
            }}
            // API-reported total context and cache read per observed turn (tokens × 3.5)
            let apiValues = [];
            let cacheValues = [];
            for (let i = 0; i < turns.length; i++) {{
                const t = turns[i];
                const tok = (t.input_tokens || 0) + (t.cache_read_input_tokens || 0) + (t.cache_creation_input_tokens || 0);
                const cr = t.cache_read_input_tokens || 0;
                const observed = tok > 0;
                apiValues.push(observed ? tok * 3.5 : null);
                cacheValues.push(observed ? cr * 3.5 : null);
            }}
            const apiMax = Math.max(...apiValues.filter(v => v !== null), 1);
            const maxCum = Math.max(...cumFullValues, apiMax, 1);
            const maxVol2 = maxVol;

            // Grid lines & left Y-axis (per-turn volume)
            ctx.strokeStyle = C_GRID;
            ctx.lineWidth = 1;
            ctx.fillStyle = '#f5f5f7';
            ctx.font = 'bold 11px -apple-system, sans-serif';
            ctx.textAlign = 'right';
            const yTicks = 5;
            for (let i = 0; i <= yTicks; i++) {{
                const y = pad.top + cH - (i / yTicks) * cH;
                ctx.beginPath();
                ctx.moveTo(pad.left, y);
                ctx.lineTo(W - pad.right, y);
                ctx.stroke();
                ctx.fillText(fmtChars(Math.round(maxVol * i / yTicks)), pad.left - 8, y + 3);
            }}
            // Left axis title
            ctx.save();
            ctx.fillStyle = '#f5f5f7';
            ctx.font = 'bold 11px -apple-system, sans-serif';
            ctx.textAlign = 'center';
            ctx.translate(12, pad.top + cH / 2);
            ctx.rotate(-Math.PI / 2);
            ctx.fillText('per-turn chars', 0, 0);
            ctx.restore();

            // Right Y-axis (cumulative input)
            ctx.textAlign = 'left';
            ctx.fillStyle = 'rgba(245,245,247,0.5)';
            ctx.font = 'bold 11px -apple-system, sans-serif';
            for (let i = 0; i <= yTicks; i++) {{
                const y = pad.top + cH - (i / yTicks) * cH;
                ctx.fillText(fmtChars(Math.round(maxCum * i / yTicks)), W - pad.right + 8, y + 3);
            }}
            // Right axis title
            ctx.save();
            ctx.fillStyle = 'rgba(245,245,247,0.5)';
            ctx.font = 'bold 11px -apple-system, sans-serif';
            ctx.textAlign = 'center';
            ctx.translate(W - 6, pad.top + cH / 2);
            ctx.rotate(Math.PI / 2);
            ctx.fillText('cumulative input', 0, 0);
            ctx.restore();

            // Stacked bars
            for (let c = 0; c < nCols; c++) {{
                const x = pad.left + c * gap + (gap - barW) / 2;
                let y = pad.top + cH;
                const segs = columns[c];
                for (let s = 0; s < segs.length; s++) {{
                    const segH = (segs[s].value / maxVol) * cH;
                    if (segH < 0.5) continue;
                    const isBottom = (s === 0);
                    const isTop = (s === segs.length - 1) || segs.slice(s + 1).every(sg => sg.value === 0);
                    const r = [
                        isTop ? 3 : 0, isTop ? 3 : 0,
                        isBottom ? 3 : 0, isBottom ? 3 : 0
                    ];

                    const ti = c - 1;
                    const turnStripped = c > 0 ? (turns[ti].stripped_tool || 0) : 0;
                    const turnStrippedThink = c > 0 ? (turns[ti].stripped_thinking || 0) : 0;
                    const isStrippedToolSeg = turnStripped > 0 && (segs[s].type === 'tool_result' || segs[s].type === 'bash' || segs[s].type === 'stale_read' || segs[s].type === 'image');
                    const isStrippedThinking = turnStrippedThink > 0 && segs[s].type === 'thinking';
                    const isHatched = isStrippedToolSeg || isStrippedThinking;

                    if (isHatched) {{
                        // Hatched fill for content removed by optimizer
                        ctx.save();
                        ctx.beginPath();
                        ctx.roundRect(x, y - segH, barW, segH, r);
                        ctx.fillStyle = segs[s].color;
                        ctx.globalAlpha = 0.2;
                        ctx.fill();
                        ctx.clip();
                        ctx.strokeStyle = segs[s].color;
                        ctx.globalAlpha = 0.55;
                        ctx.lineWidth = 1.5;
                        const step = 5;
                        for (let d = -barW; d < segH + barW; d += step) {{
                            ctx.beginPath();
                            ctx.moveTo(x, (y - segH) + d);
                            ctx.lineTo(x + barW, (y - segH) + d - barW);
                            ctx.stroke();
                        }}
                        ctx.restore();
                    }} else {{
                        ctx.fillStyle = segs[s].color;
                        ctx.beginPath();
                        ctx.roundRect(x, y - segH, barW, segH, r);
                        ctx.fill();
                    }}

                    // Add thin separator between adjacent image segments
                    if (segs[s].type === 'image' && s > 0 && segs[s-1].type === 'image') {{
                        ctx.strokeStyle = 'rgba(0,0,0,0.4)';
                        ctx.lineWidth = 0.5;
                        ctx.beginPath();
                        ctx.moveTo(x, y);
                        ctx.lineTo(x + barW, y);
                        ctx.stroke();
                    }}

                    // Record hit region for all segments
                    hitRegions.push({{
                        x: x, y: y - segH, w: barW, h: segH,
                        type: segs[s].type,
                        value: segs[s].value,
                        colIndex: c,
                        turnIndex: c > 0 ? c - 1 : -1,
                        info: segs[s].imageInfo || null,
                        imageIndex: segs[s].imageIndex,
                        stripped: isHatched,
                    }});

                    y -= segH;
                }}
            }}

            // Separator after overhead
            const sepX = pad.left + gap - 2;
            ctx.strokeStyle = 'rgba(255,255,255,0.12)';
            ctx.lineWidth = 1;
            ctx.setLineDash([4, 3]);
            ctx.beginPath();
            ctx.moveTo(sepX, pad.top);
            ctx.lineTo(sepX, pad.top + cH);
            ctx.stroke();
            ctx.setLineDash([]);

            // Vertical threshold line — show at first turn that has stripping
            if (hasAnyOpt) {{
                let firstStripped = -1;
                for (let i = 0; i < turns.length; i++) {{
                    if ((turns[i].stripped_tool || 0) + (turns[i].stripped_thinking || 0) > 0) {{
                        firstStripped = i;
                        break;
                    }}
                }}
                if (firstStripped >= 0) {{
                    const threshX = pad.left + firstStripped * gap + gap;
                    ctx.strokeStyle = '#5e5ce6';
                    ctx.lineWidth = 1;
                    ctx.globalAlpha = 0.5;
                    ctx.setLineDash([6, 4]);
                    ctx.beginPath();
                    ctx.moveTo(threshX, pad.top);
                    ctx.lineTo(threshX, pad.top + cH);
                    ctx.stroke();
                    ctx.setLineDash([]);
                    ctx.fillStyle = '#5e5ce6';
                    ctx.font = '8px -apple-system, sans-serif';
                    ctx.textAlign = 'center';
                    ctx.fillText('optimize cutoff', threshX, pad.top - 4);
                    ctx.globalAlpha = 1;
                }}
            }}

            // Cumulative curves: full (white) and optimized (indigo)
            if (cumFullValues.length > 0) {{
                // Full cumulative
                ctx.strokeStyle = C_CUM;
                ctx.lineWidth = 1.5;
                ctx.globalAlpha = 0.7;
                ctx.beginPath();
                for (let i = 0; i < cumFullValues.length; i++) {{
                    const x = pad.left + (i + 1) * gap + gap / 2;
                    const y = pad.top + cH - (cumFullValues[i] / maxCum) * cH;
                    if (i === 0) ctx.moveTo(x, y);
                    else ctx.lineTo(x, y);
                }}
                ctx.stroke();
                ctx.fillStyle = C_CUM;
                for (let i = 0; i < cumFullValues.length; i++) {{
                    const x = pad.left + (i + 1) * gap + gap / 2;
                    const y = pad.top + cH - (cumFullValues[i] / maxCum) * cH;
                    ctx.beginPath();
                    ctx.arc(x, y, 2.5, 0, Math.PI * 2);
                    ctx.fill();
                }}
                ctx.globalAlpha = 1;

                // Optimized cumulative
                ctx.strokeStyle = C_CUM_OPT;
                ctx.lineWidth = 1.5;
                ctx.globalAlpha = 0.7;
                ctx.beginPath();
                for (let i = 0; i < cumOptValues.length; i++) {{
                    const x = pad.left + (i + 1) * gap + gap / 2;
                    const y = pad.top + cH - (cumOptValues[i] / maxCum) * cH;
                    if (i === 0) ctx.moveTo(x, y);
                    else ctx.lineTo(x, y);
                }}
                ctx.stroke();
                ctx.fillStyle = C_CUM_OPT;
                for (let i = 0; i < cumOptValues.length; i++) {{
                    const x = pad.left + (i + 1) * gap + gap / 2;
                    const y = pad.top + cH - (cumOptValues[i] / maxCum) * cH;
                    ctx.beginPath();
                    ctx.arc(x, y, 2.5, 0, Math.PI * 2);
                    ctx.fill();
                }}
                ctx.globalAlpha = 1;

                // API-reported total context (red) — right axis, same scale as white/purple
                if (apiValues.some(v => v !== null)) {{
                    ctx.strokeStyle = C_API;
                    ctx.lineWidth = 1.5;
                    ctx.globalAlpha = 0.85;
                    ctx.beginPath();
                    let started = false;
                    for (let i = 0; i < apiValues.length; i++) {{
                        if (apiValues[i] === null) {{ started = false; continue; }}
                        const x = pad.left + (i + 1) * gap + gap / 2;
                        const y = pad.top + cH - (apiValues[i] / maxCum) * cH;
                        if (!started) {{ ctx.moveTo(x, y); started = true; }}
                        else ctx.lineTo(x, y);
                    }}
                    ctx.stroke();
                    ctx.fillStyle = C_API;
                    for (let i = 0; i < apiValues.length; i++) {{
                        if (apiValues[i] === null) continue;
                        const x = pad.left + (i + 1) * gap + gap / 2;
                        const y = pad.top + cH - (apiValues[i] / maxCum) * cH;
                        ctx.beginPath();
                        ctx.arc(x, y, 2.5, 0, Math.PI * 2);
                        ctx.fill();
                        hitRegions.push({{
                            x: x - 6, y: y - 6, w: 12, h: 12,
                            type: 'api_usage',
                            value: apiValues[i],
                            colIndex: i + 1,
                            turnIndex: i,
                            info: null,
                            stripped: false,
                        }});
                    }}
                    ctx.globalAlpha = 1;
                }}

                // Cache read (green) — same axis
                if (cacheValues.some(v => v !== null)) {{
                    ctx.strokeStyle = C_CACHE;
                    ctx.lineWidth = 1.5;
                    ctx.globalAlpha = 0.7;
                    ctx.beginPath();
                    let started = false;
                    for (let i = 0; i < cacheValues.length; i++) {{
                        if (cacheValues[i] === null) {{ started = false; continue; }}
                        const x = pad.left + (i + 1) * gap + gap / 2;
                        const y = pad.top + cH - (cacheValues[i] / maxCum) * cH;
                        if (!started) {{ ctx.moveTo(x, y); started = true; }}
                        else ctx.lineTo(x, y);
                    }}
                    ctx.stroke();
                    ctx.fillStyle = C_CACHE;
                    for (let i = 0; i < cacheValues.length; i++) {{
                        if (cacheValues[i] === null) continue;
                        const x = pad.left + (i + 1) * gap + gap / 2;
                        const y = pad.top + cH - (cacheValues[i] / maxCum) * cH;
                        ctx.beginPath();
                        ctx.arc(x, y, 2.5, 0, Math.PI * 2);
                        ctx.fill();
                    }}
                    ctx.globalAlpha = 1;
                }}
            }}

            // X-axis labels
            ctx.fillStyle = '#f5f5f7';
            ctx.font = 'bold 11px -apple-system, sans-serif';
            ctx.textAlign = 'center';
            const maxLabels = Math.floor(cW / 40);
            const step = Math.max(1, Math.ceil(nCols / maxLabels));
            for (let c = 0; c < nCols; c += step) {{
                const x = pad.left + c * gap + gap / 2;
                ctx.save();
                ctx.translate(x, pad.top + cH + 10);
                ctx.rotate(-0.5);
                ctx.fillText(labels[c], 0, 0);
                ctx.restore();
            }}

            // Cache control markers
            ctx.font = 'bold 9px -apple-system, sans-serif';
            ctx.textAlign = 'center';
            const markerSpacing = 4;
            for (let c = 1; c < nCols; c++) {{
                const turnIdx = c - 1;
                if (turnIdx >= turns.length) continue;
                const t = turns[turnIdx];
                if (!t.cache_control_types || t.cache_control_types.length === 0) continue;

                const x = pad.left + c * gap + gap / 2;
                let markerY = pad.top + cH + 30;

                for (let i = 0; i < t.cache_control_types.length; i++) {{
                    const type = t.cache_control_types[i];
                    const color = CACHE_COLORS[type] || '#ccc';

                    // Draw small colored square
                    const size = 5;
                    ctx.fillStyle = color;
                    ctx.fillRect(x - size/2, markerY - size/2, size, size);

                    // Label
                    ctx.fillStyle = color;
                    ctx.fillText(type, x, markerY + 10);

                    markerY += markerSpacing + 10;
                }}
            }}
        }}

        // Hover tooltip
        canvas.addEventListener('mousemove', (e) => {{
            const rect = canvas.getBoundingClientRect();
            const mx = e.clientX - rect.left;
            const my = e.clientY - rect.top;

            let hit = null;
            for (const r of hitRegions) {{
                if (mx >= r.x && mx <= r.x + r.w && my >= r.y && my <= r.y + r.h) {{
                    hit = r;
                    break;
                }}
            }}

            if (hit) {{
                tooltip.innerHTML = buildTooltip(hit);
                tooltip.style.display = 'block';
                let tx = e.clientX + 12;
                let ty = e.clientY + 12;
                const tw = tooltip.offsetWidth;
                const th = tooltip.offsetHeight;
                if (tx + tw > window.innerWidth - 10) tx = e.clientX - tw - 12;
                if (ty + th > window.innerHeight - 10) ty = e.clientY - th - 12;
                tooltip.style.left = tx + 'px';
                tooltip.style.top = ty + 'px';
                canvas.style.cursor = 'pointer';
            }} else {{
                tooltip.style.display = 'none';
                canvas.style.cursor = 'default';
            }}
        }});

        canvas.addEventListener('mouseleave', () => {{
            tooltip.style.display = 'none';
            canvas.style.cursor = 'default';
        }});

        draw();
        window.addEventListener('resize', draw);

        // ── Cache Simulator Chart ──────────────────────────────────────
        const cacheCanvas = document.getElementById('cache-chart');
        const cacheCtx = cacheCanvas.getContext('2d');

        function drawCache() {{
            // Check if any turn has cache sim data
            const hasData = turns.some(t => (t.cache_sim_total || 0) > 0);
            if (!hasData) return;

            const dpr = window.devicePixelRatio || 1;
            const rect = cacheCanvas.parentElement.getBoundingClientRect();
            const W = rect.width;
            const H = rect.height;
            cacheCanvas.width = W * dpr;
            cacheCanvas.height = H * dpr;
            cacheCtx.setTransform(dpr, 0, 0, dpr, 0, 0);

            // Use same column layout as main chart
            const nCols = 1 + turns.length;
            const pad = {{ top: 8, right: 60, bottom: 4, left: 60 }};
            const cW = W - pad.left - pad.right;
            const cH = H - pad.top - pad.bottom;
            const gap = cW / nCols;
            const barW = Math.max(4, Math.min(40, gap * 0.7));

            const maxBytes = Math.max(...turns.map(t => t.cache_sim_total || 0), 1);

            cacheCtx.clearRect(0, 0, W, H);

            // Y-axis label (left — bytes)
            cacheCtx.save();
            cacheCtx.fillStyle = '#86868b';
            cacheCtx.font = '9px -apple-system, sans-serif';
            cacheCtx.textAlign = 'center';
            cacheCtx.translate(10, pad.top + cH / 2);
            cacheCtx.rotate(-Math.PI / 2);
            cacheCtx.fillText('cache sim (bytes)', 0, 0);
            cacheCtx.restore();

            // Y-axis label (right — percentage)
            cacheCtx.save();
            cacheCtx.fillStyle = '#86868b';
            cacheCtx.font = '9px -apple-system, sans-serif';
            cacheCtx.textAlign = 'center';
            cacheCtx.translate(W - 8, pad.top + cH / 2);
            cacheCtx.rotate(Math.PI / 2);
            cacheCtx.fillText('prefix %', 0, 0);
            cacheCtx.restore();

            // Grid lines
            cacheCtx.strokeStyle = 'rgba(255,255,255,0.06)';
            cacheCtx.lineWidth = 0.5;
            for (let i = 0; i <= 4; i++) {{
                const y = pad.top + (cH / 4) * i;
                cacheCtx.beginPath();
                cacheCtx.moveTo(pad.left, y);
                cacheCtx.lineTo(pad.left + cW, y);
                cacheCtx.stroke();
            }}

            // Left Y ticks (bytes)
            cacheCtx.fillStyle = '#86868b';
            cacheCtx.font = '9px -apple-system, sans-serif';
            cacheCtx.textAlign = 'right';
            for (let i = 0; i <= 4; i++) {{
                const y = pad.top + (cH / 4) * i;
                const val = maxBytes * (1 - i / 4);
                let label;
                if (val >= 1e6) label = (val / 1e6).toFixed(1) + 'M';
                else if (val >= 1e3) label = (val / 1e3).toFixed(1) + 'k';
                else label = val.toFixed(0);
                cacheCtx.fillText(label, pad.left - 6, y + 3);
            }}

            // Right Y ticks (percentage)
            cacheCtx.textAlign = 'left';
            for (let i = 0; i <= 4; i++) {{
                const y = pad.top + (cH / 4) * i;
                const pct = (1 - i / 4) * 100;
                cacheCtx.fillText(pct.toFixed(0) + '%', pad.left + cW + 6, y + 3);
            }}

            // Bars per turn — same column index as main chart
            for (let ti = 0; ti < turns.length; ti++) {{
                const c = ti + 1;  // column index (0 = overhead, 1+ = turns)
                const x = pad.left + c * gap + (gap - barW) / 2;
                const total = turns[ti].cache_sim_total || 0;
                const prefix = turns[ti].cache_sim_prefix || 0;

                // Total bar (dim gray)
                const totalH = (total / maxBytes) * cH;
                cacheCtx.fillStyle = 'rgba(134,134,139,0.3)';
                cacheCtx.fillRect(x, pad.top + cH - totalH, barW, totalH);

                // Prefix bar (green)
                const prefixH = (prefix / maxBytes) * cH;
                cacheCtx.fillStyle = '#30d158';
                cacheCtx.globalAlpha = 0.7;
                cacheCtx.fillRect(x, pad.top + cH - prefixH, barW, prefixH);
                cacheCtx.globalAlpha = 1;
            }}

            // Percentage line
            cacheCtx.strokeStyle = '#5e5ce6';
            cacheCtx.lineWidth = 1.5;
            cacheCtx.globalAlpha = 0.9;
            cacheCtx.beginPath();
            for (let ti = 0; ti < turns.length; ti++) {{
                const c = ti + 1;
                const x = pad.left + c * gap + gap / 2;
                const total = turns[ti].cache_sim_total || 0;
                const pct = total > 0 ? (turns[ti].cache_sim_prefix || 0) / total : 0;
                const y = pad.top + cH - pct * cH;
                if (ti === 0) cacheCtx.moveTo(x, y);
                else cacheCtx.lineTo(x, y);
            }}
            cacheCtx.stroke();

            // Percentage dots
            cacheCtx.fillStyle = '#5e5ce6';
            for (let ti = 0; ti < turns.length; ti++) {{
                const c = ti + 1;
                const x = pad.left + c * gap + gap / 2;
                const total = turns[ti].cache_sim_total || 0;
                const pct = total > 0 ? (turns[ti].cache_sim_prefix || 0) / total : 0;
                const y = pad.top + cH - pct * cH;
                cacheCtx.beginPath();
                cacheCtx.arc(x, y, 2.5, 0, Math.PI * 2);
                cacheCtx.fill();
            }}
            cacheCtx.globalAlpha = 1;
        }}

        drawCache();
        window.addEventListener('resize', drawCache);
        </script>
    </body>
    </html>
    """
