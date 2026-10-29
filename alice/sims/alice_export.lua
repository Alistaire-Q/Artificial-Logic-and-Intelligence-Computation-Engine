-- ALICE DCS export betiği: uçuş verisini OKUR ve yerel UDP ile ALICE'e gönderir.
-- Sesli kokpit komutları için sadece 127.0.0.1'den, sadece aşağıdaki izin listesindeki komutları kabul eder
-- (teker, flap, hava freni, kanca, chaff, flare, telsiz frekansı); silah atma/bırakma gibi komut yoktur. `alice dcs install` bunu Saved Games\DCS\Scripts altına koyar
-- ve Export.lua'ya tek satır ekler. Diğer export betikleriyle (SRS, TacView, Helios) birlikte çalışır.
do
  -- Export ortamında LuaSocket'in yolu tanımlı değil: diğer export betikleri (SRS, TacView) gibi ekle.
  package.path = package.path .. ";.\\LuaSocket\\?.lua"
  package.cpath = package.cpath .. ";.\\LuaSocket\\?.dll"
  local function note(msg)
    if log and log.write then pcall(log.write, "ALICE", log.INFO, msg) end
  end
  local ok_socket, socket = pcall(require, "socket")
  if not ok_socket then
    note("LuaSocket yuklenemedi: " .. tostring(socket))
    return
  end
  note("export betigi yuklendi")

  local HOST, PORT, HZ = "127.0.0.1", 41230, 10
  local udp, next_send = nil, 0
  local cmd_sock, ack = nil, nil
  -- İzin verilen LoSetCommand kimlikleri (DCS'in genel komut numaraları)
  local ALLOWED = { gear = 68, hook = 69, flaps = 72, airbrake = 73, flare = 357, chaff = 358 }
  local next_radio, radios = 0, nil

  local prev_start, prev_after, prev_stop = LuaExportStart, LuaExportAfterNextFrame, LuaExportStop

  local function try(f, ...)
    if type(f) ~= "function" then return nil end
    local ok, r = pcall(f, ...)
    if ok then return r end
    return nil
  end

  local function esc(s)
    return (tostring(s):gsub('[%c"\\]', function(c) return string.format("\\u%04x", c:byte()) end))
  end

  local function encode(t)
    local parts = {}
    for k, v in pairs(t) do
      local tv = type(v)
      if tv == "number" then
        if v == v and v ~= math.huge and v ~= -math.huge then
          parts[#parts + 1] = string.format('"%s":%.5g', k, v)
        end
      elseif tv == "string" then
        parts[#parts + 1] = string.format('"%s":"%s"', k, esc(v))
      elseif tv == "boolean" then
        parts[#parts + 1] = string.format('"%s":%s', k, tostring(v))
      end
    end
    return "{" .. table.concat(parts, ",") .. "}"
  end

  local function send(t)
    if udp then pcall(udp.send, udp, encode(t)) end
  end

  local function collect()
    local d = { t = try(LoGetModelTime) }
    local me = try(LoGetSelfData)
    if not me then
      -- Çok oyunculuda sunucu veri paylaşımını kısıtlamış olabilir.
      d.restricted = true
      return d
    end
    d.aircraft = me.Name
    if me.LatLongAlt then d.alt = me.LatLongAlt.Alt; d.lat = me.LatLongAlt.Lat; d.lon = me.LatLongAlt.Long end
    if me.Position then d.x = me.Position.x; d.z = me.Position.z end
    d.heading = me.Heading
    d.ias = try(LoGetIndicatedAirSpeed)
    d.tas = try(LoGetTrueAirSpeed)
    d.agl = try(LoGetAltitudeAboveGroundLevel)
    d.vv = try(LoGetVerticalVelocity)
    d.aoa = try(LoGetAngleOfAttack)
    local acc = try(LoGetAccelerationUnits)
    if acc then d.g = acc.y end
    local eng = try(LoGetEngineInfo)
    if eng then
      d.fuel_int = eng.fuel_internal
      d.fuel_ext = eng.fuel_external
      if eng.FuelConsumption then d.burn = (eng.FuelConsumption.left or 0) + (eng.FuelConsumption.right or 0) end
    end
    local sn = try(LoGetSnares)
    if sn then d.chaff = sn.chaff; d.flare = sn.flare end
    local mech = try(LoGetMechInfo)
    if mech then
      if mech.gear then d.gear = mech.gear.value end
      if mech.flaps then d.flaps = mech.flaps.value end
      if mech.speedbrakes then d.speedbrake = mech.speedbrakes.value end
      if mech.hook then d.hook = mech.hook.value end
    end
    d.pilot = try(LoGetPilotName)

    -- Ayrıntı: hepsi kendi uçağının, kokpitte de görülen verisi (resmi Export API; başka uçakların konumu yok).
    d.mach = try(LoGetMachNumber)
    d.mag_heading = try(LoGetMagneticYaw)
    d.radalt = try(LoGetRadarAltimeter)
    d.slip = try(LoGetSlipBallPosition)
    local ok_pby, pitch, bank = pcall(LoGetADIPitchBankYaw)
    if ok_pby then d.pitch = pitch; d.bank = bank end
    local wind = try(LoGetVectorWindVelocity)
    if wind then d.wind_x = wind.x; d.wind_z = wind.z end
    if eng then
      if eng.RPM then d.rpm_l = eng.RPM.left; d.rpm_r = eng.RPM.right end
      if eng.Temperature then d.egt_l = eng.Temperature.left; d.egt_r = eng.Temperature.right end
    end
    local route = try(LoGetRoute)
    if route and route.goto_point and route.goto_point.world_point then
      d.wp_x = route.goto_point.world_point.x
      d.wp_z = route.goto_point.world_point.z
      d.wp_num = route.goto_point.point_num
    end
    local mcp = try(LoGetMCPState)
    if mcp then
      local on = {}
      for k, v in pairs(mcp) do if v == true then on[#on + 1] = k end end
      table.sort(on)
      d.warnings = table.concat(on, ",")
    end
    local pl = try(LoGetPayloadInfo)
    if pl and pl.Stations then
      local counts, order = {}, {}
      for _, st in pairs(pl.Stations) do
        if st.count and st.count > 0 and st.weapon then
          local w = st.weapon
          local name = (LoGetNameByType and try(LoGetNameByType, w.level1, w.level2, w.level3, w.level4)) or "?"
          if not counts[name] then order[#order + 1] = name; counts[name] = 0 end
          counts[name] = counts[name] + st.count
        end
      end
      local parts = {}
      for _, n in ipairs(order) do parts[#parts + 1] = n .. " x" .. counts[n] end
      d.weapons = table.concat(parts, ", ")
      if pl.Cannon then d.gun = pl.Cannon.shells end
    end
    -- Radar uyarı alıcısı (RWR): kokpitteki ekranda görülen yayıcılar. "kimlik|açı(rad)|sinyal|tip|güç" ; ile
    local tws = try(LoGetTWSInfo)
    if tws and tws.Emitters then
      local parts = {}
      for _, e in pairs(tws.Emitters) do
        local name = "?"
        if LoGetNameByType and e.Type then
          name = try(LoGetNameByType, e.Type.level1, e.Type.level2, e.Type.level3, e.Type.level4) or "?"
        end
        name = (tostring(name):gsub("[|;]", " "))
        parts[#parts + 1] = string.format("%s|%.3f|%s|%s|%.2f", tostring(e.ID), e.Azimuth or 0,
          tostring(e.SignalType or ""), name, e.Power or 0)
      end
      d.rwr = table.concat(parts, ";")
    end
    d.ils_glide = try(LoGetGlideDeviation)
    d.ils_side = try(LoGetSideDeviation)
    -- Telsiz frekansları: kokpit cihazlarından get_frequency verenler (her modülde aynı yöntem; 2 sn'de bir).
    if GetDevice and (d.t or 0) >= next_radio then
      next_radio = (d.t or 0) + 2
      local seen, list = {}, {}
      for i = 1, 100 do
        local dev = GetDevice(i)
        if dev and dev.get_frequency then
          local ok, f = pcall(dev.get_frequency, dev)
          if ok and type(f) == "number" and f > 1e6 and not seen[f] then
            seen[f] = true
            list[#list + 1] = string.format("%.3f", f / 1e6)
          end
        end
      end
      radios = table.concat(list, ",")
    end
    d.radios = radios
    d.ack = ack
    local nav = try(LoGetNavigationInfo)
    if nav and nav.SystemMode then d.mode = tostring(nav.SystemMode.master or "") .. "/" .. tostring(nav.SystemMode.submode or "") end
    return d
  end

  -- Telsiz: frekansı aynı bantta olan ilk cihaza (UHF 225-400, VHF 108-174 / 30-88 MHz) set_frequency.
  local function band(mhz)
    if mhz >= 225 and mhz <= 400 then return "uhf" end
    if mhz >= 108 and mhz < 174 then return "vhf" end
    if mhz >= 30 and mhz < 88 then return "fm" end
    return nil
  end

  local function set_freq(mhz)
    local want = band(mhz)
    if not want or not GetDevice then return "err|bu frekans bandı desteklenmiyor" end
    for i = 1, 100 do
      local dev = GetDevice(i)
      if dev and dev.get_frequency and dev.set_frequency then
        local ok, f = pcall(dev.get_frequency, dev)
        if ok and type(f) == "number" and band(f / 1e6) == want then
          local ok2 = pcall(dev.set_frequency, dev, math.floor(mhz * 1e6 + 0.5))
          if ok2 then return string.format("ok|%.3f cihaz %d", mhz, i) end
        end
      end
    end
    return "err|bu uçakta o bantta ayarlanabilen telsiz bulunamadı"
  end

  local function handle(msg)
    local seq, kind, arg = msg:match("^(%d+)|(%a+)|?(.*)$")
    if not seq then return end
    local r
    if kind == "cmd" and ALLOWED[arg] then
      local ok = pcall(LoSetCommand, ALLOWED[arg])
      r = ok and ("ok|" .. arg) or "err|komut çalışmadı"
    elseif kind == "freq" and tonumber(arg) then
      r = set_freq(tonumber(arg))
    else
      r = "err|izin verilmeyen komut"
    end
    ack = seq .. "|" .. r
  end

  local function poll_commands()
    if not cmd_sock then return end
    for _ = 1, 5 do
      local msg, ip = cmd_sock:receivefrom()
      if not msg then return end
      if ip == "127.0.0.1" then pcall(handle, msg) end
    end
  end

  function LuaExportStart()
    if prev_start then pcall(prev_start) end
    udp = socket.udp()
    udp:settimeout(0)
    udp:setpeername(HOST, PORT)
    cmd_sock = socket.udp()
    if cmd_sock and cmd_sock:setsockname("127.0.0.1", PORT + 1) then cmd_sock:settimeout(0) else cmd_sock = nil end
    note("veri gonderiliyor: " .. HOST .. ":" .. PORT)
    send({ event = "start" })
  end

  function LuaExportAfterNextFrame()
    if prev_after then pcall(prev_after) end
    local now = try(LoGetModelTime) or 0
    pcall(poll_commands)
    if now >= next_send then
      next_send = now + 1 / HZ
      local ok, d = pcall(collect)
      if ok and d then send(d) end
    end
  end

  function LuaExportStop()
    send({ event = "stop" })
    if udp then pcall(udp.close, udp); udp = nil end
    if cmd_sock then pcall(cmd_sock.close, cmd_sock); cmd_sock = nil end
    if prev_stop then pcall(prev_stop) end
  end
end
