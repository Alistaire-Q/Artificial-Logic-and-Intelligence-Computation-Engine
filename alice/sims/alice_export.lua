-- ALICE DCS export betiği: uçuş verisini sadece OKUR ve yerel UDP ile ALICE'e gönderir.
-- Oyuna hiçbir komut göndermez. `alice dcs install` bunu Saved Games\DCS\Scripts altına koyar
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
    local nav = try(LoGetNavigationInfo)
    if nav and nav.SystemMode then d.mode = tostring(nav.SystemMode.master or "") .. "/" .. tostring(nav.SystemMode.submode or "") end
    return d
  end

  function LuaExportStart()
    if prev_start then pcall(prev_start) end
    udp = socket.udp()
    udp:settimeout(0)
    udp:setpeername(HOST, PORT)
    note("veri gonderiliyor: " .. HOST .. ":" .. PORT)
    send({ event = "start" })
  end

  function LuaExportAfterNextFrame()
    if prev_after then pcall(prev_after) end
    local now = try(LoGetModelTime) or 0
    if now >= next_send then
      next_send = now + 1 / HZ
      local ok, d = pcall(collect)
      if ok and d then send(d) end
    end
  end

  function LuaExportStop()
    send({ event = "stop" })
    if udp then pcall(udp.close, udp); udp = nil end
    if prev_stop then pcall(prev_stop) end
  end
end
