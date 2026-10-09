-- ALICE DCS export betiği: uçuş verisini sadece OKUR ve yerel UDP ile ALICE'e gönderir.
-- Oyuna hiçbir komut göndermez. `alice dcs install` bunu Saved Games\DCS\Scripts altına koyar
-- ve Export.lua'ya tek satır ekler. Diğer export betikleriyle (SRS, TacView, Helios) birlikte çalışır.
do
  local ok_socket, socket = pcall(require, "socket")
  if not ok_socket then return end

  local HOST, PORT, HZ = "127.0.0.1", 41230, 10
  local udp, next_send = nil, 0

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
    if mech and mech.gear then d.gear = mech.gear.value end
    d.pilot = try(LoGetPilotName)
    return d
  end

  function LuaExportStart()
    if prev_start then pcall(prev_start) end
    udp = socket.udp()
    udp:settimeout(0)
    udp:setpeername(HOST, PORT)
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
