-- ALICE kanat adamı hook'u: sesle yönetilen yapay zekâ kanat adamları (DCS GameGUI hook'u, SRS/Tacview gibi).
-- `alice dcs install` (ya da çekirdek açılışta) bunu Saved Games\DCS\Scripts\Hooks altına koyar.
--
-- Güvenlik:
--   * Sadece 127.0.0.1'de UDP dinler; başka adresten gelen paket yok sayılır.
--   * Ağdan gelen metin ASLA Lua olarak çalıştırılmaz. Sadece "ALICE1|fiil|sıra|hedef|yön|grup" satırı kabul
--     edilir; her alan izin listesi / sayı / sınırlı karakter olarak doğrulanır, görev Lua'sını bu dosya kurar.
--   * Sadece tek oyunculuda ya da biz sunucuyken çalışır; başkasının sunucusunda hiçbir şey yapmaz.
--   * Komut hızı sınırlıdır; her hata sessizce dcs.log'a yazılır, oyunu bozmaz.
do
  package.path = package.path .. ";.\\LuaSocket\\?.lua"
  package.cpath = package.cpath .. ";.\\LuaSocket\\?.dll"

  local ALICE_PORT = 41231
  local ALICE_NAMES = {}
  local MAX_LINE, MAX_PER_FRAME, MIN_GAP = 200, 4, 0.5

  local function note(msg)
    if log and log.write then pcall(log.write, "ALICE", log.INFO, tostring(msg)) end
  end

  local ok_socket, socket = pcall(require, "socket")
  if not ok_socket then
    note("kanat hook'u: LuaSocket yuklenemedi: " .. tostring(socket))
    return
  end

  local VERBS = { attack = true, rejoin = true, hold = true, rtb = true }
  local TARGETS = { sam = true, air = true, ground = true, ship = true, any = true }
  local DIRS = { right = true, left = true, front = true, behind = true, any = true }

  -- Görev ortamında çalışan SABİT kod. Ağdan gelen hiçbir şey buraya metin olarak eklenmez; sadece aşağıdaki
  -- ALICE_WM.run(...) çağrısına doğrulanmış argümanlar string.format("%q"/"%d") ile geçer.
  local MISSION_LIB = [==[
if not ALICE_WM then
  ALICE_WM = {}
  local W = ALICE_WM
  local ORD = { "Birinci", "İkinci", "Üçüncü", "Dördüncü", "Beşinci", "Altıncı", "Yedinci", "Sekizinci", "Dokuzuncu" }
  local TNAME = { sam = "SAM", air = "düşman uçağı", ground = "kara hedefi", ship = "gemi", any = "düşman" }
  local TDAT = { sam = "SAM'e", air = "düşman uçağına", ground = "kara hedefine", ship = "gemiye", any = "düşmana" }
  local DNAME = { right = "sağda", left = "solda", front = "önde", behind = "arkada", any = "" }
  local AD = { "SAM SR", "SAM TR", "SAM LL", "AAA", "Air Defence" }

  local function say(text) trigger.action.outText(text, 10) end

  local function player()
    local u = world.getPlayer and world.getPlayer()
    if u and u:isExist() then return u end
    for _, side in ipairs({ coalition.side.BLUE, coalition.side.RED }) do
      for _, p in ipairs(coalition.getPlayers(side) or {}) do
        if p and p:isExist() then return p end
      end
    end
  end

  local function lead(g)
    if not (g and g:isExist()) then return nil end
    for _, u in ipairs(g:getUnits() or {}) do
      if u and u:isExist() and u:getLife() > 0 then return u end
    end
  end

  local function dist(a, b) return math.sqrt((a.x - b.x) ^ 2 + (a.z - b.z) ^ 2) end

  local function has_human(g)
    for _, u in ipairs(g:getUnits() or {}) do
      if u.getPlayerName and u:getPlayerName() then return true end
    end
    return false
  end

  -- Kanat adamları: [sims] dcs_wingmen verilmişse o sırayla; yoksa oyuncunun koalisyonundaki, oyuncunun grubu
  -- olmayan, içinde insan olmayan AI uçak/helikopter grupları, oyuncuya uzaklığa göre.
  local function wingmen(pu, names)
    local out, pg = {}, pu:getGroup()
    local pid = pg and pg:getID()
    if #names > 0 then
      for _, n in ipairs(names) do
        local g = Group.getByName(n)
        if g and lead(g) and g:getID() ~= pid then out[#out + 1] = g end
      end
      return out
    end
    local here, side = pu:getPoint(), pu:getCoalition()
    local cand = {}
    for _, cat in ipairs({ Group.Category.AIRPLANE, Group.Category.HELICOPTER }) do
      for _, g in ipairs(coalition.getGroups(side, cat) or {}) do
        local l = lead(g)
        if l and g:getID() ~= pid and not has_human(g) and l:inAir() then
          cand[#cand + 1] = { g = g, d = dist(l:getPoint(), here) }
        end
      end
    end
    table.sort(cand, function(a, b) return a.d < b.d end)
    for _, c in ipairs(cand) do out[#out + 1] = c.g end
    return out
  end

  -- Sıra: oyuncu lider (1) sayılır; "İkinci" = listedeki ilk (en yakın) kanat adamı, "Üçüncü" = ikincisi...
  -- "Birinci" de ilkini seçer. 0 = hepsi. Tek kanat adamı varsa sıra ne olursa olsun o.
  local function pick(list, idx, gname)
    if gname ~= "" then
      for _, g in ipairs(list) do
        if g:getName() == gname then return { g }, { gname } end
      end
      return {}, {}
    end
    if #list == 1 then return { list[1] }, { ORD[math.max(idx, 2)] or ("Kanat " .. idx) } end
    if idx == 0 then
      local labels = {}
      for i = 1, #list do labels[i] = ORD[i + 1] or ("Kanat " .. (i + 1)) end
      return list, labels
    end
    local i = math.max(idx - 1, 1)
    if list[i] then return { list[i] }, { ORD[idx] or ("Kanat " .. idx) } end
    return {}, {}
  end

  local function rel_bearing(pu, p)
    local pos = pu:getPosition()
    local hdg = math.atan2(pos.x.z, pos.x.x)
    local here = pos.p
    local brg = math.atan2(p.z - here.z, p.x - here.x)
    local r = math.deg(brg - hdg)
    while r > 180 do r = r - 360 end
    while r <= -180 do r = r + 360 end
    return r
  end

  local function dir_ok(dir, r)
    if dir == "right" then return r >= 10 and r <= 170 end
    if dir == "left" then return r <= -10 and r >= -170 end
    if dir == "front" then return math.abs(r) <= 60 end
    if dir == "behind" then return math.abs(r) >= 120 end
    return true
  end

  local function is_ad(g)
    for _, u in ipairs(g:getUnits() or {}) do
      for _, a in ipairs(AD) do
        if u:hasAttribute(a) then return true end
      end
    end
    return false
  end

  local function find_target(pu, kind, dir)
    local side = pu:getCoalition()
    local cats = {}
    if kind == "sam" or kind == "ground" then cats = { Group.Category.GROUND }
    elseif kind == "air" then cats = { Group.Category.AIRPLANE, Group.Category.HELICOPTER }
    elseif kind == "ship" then cats = { Group.Category.SHIP }
    else cats = { Group.Category.GROUND, Group.Category.AIRPLANE, Group.Category.HELICOPTER, Group.Category.SHIP } end
    local here, best, bestd = pu:getPoint(), nil, nil
    for _, enemy in ipairs({ coalition.side.RED, coalition.side.BLUE }) do
      if enemy ~= side then
        for _, cat in ipairs(cats) do
          for _, g in ipairs(coalition.getGroups(enemy, cat) or {}) do
            local l = lead(g)
            local ok = l ~= nil
            if ok and kind == "sam" then ok = is_ad(g) end
            if ok and kind == "ground" then ok = not is_ad(g) end
            if ok then
              local p = l:getPoint()
              local d = dist(p, here)
              if dir_ok(dir, rel_bearing(pu, p)) and (not bestd or d < bestd) then best, bestd = g, d end
            end
          end
        end
      end
    end
    return best, bestd
  end

  local function nearest_base(pu, from)
    local best, bestd = nil, nil
    for _, ab in ipairs(coalition.getAirbases(pu:getCoalition()) or {}) do
      local desc = ab:getDesc()
      if desc and desc.category == Airbase.Category.AIRDROME then
        local d = dist(ab:getPoint(), from)
        if not bestd or d < bestd then best, bestd = ab, d end
      end
    end
    return best
  end

  function W.run(verb, idx, kind, dir, gname, names)
    local pu = player()
    if not pu then return end
    local list, labels = pick(wingmen(pu, names), idx, gname)
    if #list == 0 then
      say(idx > 0 and ((ORD[idx] or ("Kanat " .. idx)) .. ": böyle bir kanat adamı yok") or "ALICE: kanat adamı bulunamadı")
      return
    end
    if verb == "attack" then
      local tgt, d = find_target(pu, kind, dir)
      if not tgt then
        local where = DNAME[dir] ~= "" and (DNAME[dir] .. " ") or ""
        say(labels[1] .. ": " .. where .. TNAME[kind] .. " yok")
        return
      end
      for i, g in ipairs(list) do
        local c = g:getController()
        pcall(c.setOption, c, AI.Option.Air.id.ROE, AI.Option.Air.val.ROE.OPEN_FIRE)
        c:pushTask({ id = "AttackGroup", params = { groupId = tgt:getID() } })
        say(labels[i] .. ": " .. TDAT[kind] .. " saldırıyor (" .. math.floor(d / 1000 + 0.5) .. " km)")
      end
    elseif verb == "rejoin" then
      local pg = pu:getGroup()
      for i, g in ipairs(list) do
        local side = (i % 2 == 1) and 1 or -1
        g:getController():setTask({ id = "Follow", params = { groupId = pg:getID(),
          pos = { x = -200 * i, y = 0, z = 200 * i * side }, lastWptIndexFlag = false } })
        say(labels[i] .. ": yanına geliyor")
      end
    elseif verb == "hold" then
      for i, g in ipairs(list) do
        local p = lead(g):getPoint()
        g:getController():setTask({ id = "Orbit", params = { pattern = "Circle", point = { x = p.x, y = p.z },
          altitude = p.y, speed = 180 } })
        say(labels[i] .. ": bulunduğu yerde tur atıyor")
      end
    elseif verb == "rtb" then
      for i, g in ipairs(list) do
        local p = lead(g):getPoint()
        local ab = nearest_base(pu, p)
        if not ab then
          say(labels[i] .. ": dost hava üssü bulunamadı")
        else
          local a = ab:getPoint()
          g:getController():setTask({ id = "Mission", params = { airborne = true, route = { points = {
            { type = "Turning Point", action = "Turning Point", x = p.x, y = p.z, alt = p.y, alt_type = "BARO",
              speed = 180 },
            { type = "Land", action = "Landing", x = a.x, y = a.z, alt = a.y, alt_type = "BARO", speed = 150,
              airdromeId = ab:getID() } } } } })
          say(labels[i] .. ": " .. ab:getName() .. " üssüne dönüyor")
        end
      end
    end
  end
end
]==]

  local udp, last_cmd = nil, -1000

  local function allowed()
    local ok, res = pcall(function()
      return (not DCS.isMultiplayer()) or DCS.isServer()
    end)
    return ok and res
  end

  local function now()
    local ok, t = pcall(DCS.getRealTime)
    if ok and type(t) == "number" then return t end
    return os.clock()
  end

  local function close()
    if udp then pcall(udp.close, udp) end
    udp = nil
  end

  local function open()
    close()
    if not allowed() then return end
    local s = socket.udp()
    s:settimeout(0)
    local ok, err = s:setsockname("127.0.0.1", ALICE_PORT)
    if not ok then
      note("kanat hook'u: port acilamadi: " .. tostring(err))
      pcall(s.close, s)
      return
    end
    udp = s
    note("kanat hook'u dinliyor: 127.0.0.1:" .. ALICE_PORT)
  end

  -- "ALICE1|fiil|sıra|hedef|yön|grup" -> doğrulanmış alanlar ya da nil
  local function parse(line)
    if type(line) ~= "string" or #line > MAX_LINE then return nil end
    local f = {}
    for part in (line .. "|"):gmatch("([^|]*)|") do f[#f + 1] = part end
    if f[1] ~= "ALICE1" or #f < 3 or #f > 6 then return nil end
    local verb = f[2]
    if not VERBS[verb] then return nil end
    local idx = tonumber(f[3])
    if not idx or idx ~= math.floor(idx) or idx < 0 or idx > 9 then return nil end
    local kind = (f[4] == nil or f[4] == "") and "any" or f[4]
    local dir = (f[5] == nil or f[5] == "") and "any" or f[5]
    if not TARGETS[kind] or not DIRS[dir] then return nil end
    local gname = f[6] or ""
    if #gname > 40 or not gname:match("^[%w _%-]*$") then return nil end
    return verb, idx, kind, dir, gname
  end

  local function names_lua()
    local parts = {}
    for _, n in ipairs(ALICE_NAMES) do parts[#parts + 1] = string.format("%q", tostring(n)) end
    return "{" .. table.concat(parts, ",") .. "}"
  end

  local function dispatch(verb, idx, kind, dir, gname)
    local call = string.format("ALICE_WM.run(%q, %d, %q, %q, %q, %s)", verb, idx, kind, dir, gname, names_lua())
    local chunk = MISSION_LIB .. "\n" .. call
    local res = net.dostring_in("mission", "a_do_script(" .. string.format("%q", chunk) .. ")")
    note("kanat komutu: " .. verb .. " " .. idx .. " " .. kind .. " " .. dir .. " -> " .. tostring(res))
  end

  local function poll()
    if not udp then return end
    for _ = 1, MAX_PER_FRAME do
      local data, ip = udp:receivefrom()
      if not data then return end
      if ip == "127.0.0.1" then
        local verb, idx, kind, dir, gname = parse(data)
        if not verb then
          note("kanat hook'u: gecersiz satir yok sayildi")
        elseif now() - last_cmd < MIN_GAP then
          note("kanat hook'u: hiz siniri, komut yok sayildi")
        elseif allowed() then
          last_cmd = now()
          dispatch(verb, idx, kind, dir, gname)
        end
      end
    end
  end

  local handler = {}
  function handler.onSimulationStart()
    local ok, err = pcall(open)
    if not ok then note("kanat hook'u acilis hatasi: " .. tostring(err)) end
  end
  function handler.onSimulationStop()
    pcall(close)
  end
  function handler.onSimulationFrame()
    if not udp then return end
    local ok, err = pcall(poll)
    if not ok then note("kanat hook'u hatasi: " .. tostring(err)) end
  end

  local ok, err = pcall(DCS.setUserCallbacks, handler)
  if ok then note("kanat hook'u yuklendi") else note("kanat hook'u kaydedilemedi: " .. tostring(err)) end
end
