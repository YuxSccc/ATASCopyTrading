#property strict
#property version   "0.105"
#property description "Local ATAS-to-MT5 bridge observer and guarded command executor."

input string BridgeBaseUrl = "http://127.0.0.1:8768";
input string PairTag = "";
input long BridgeMagic = 26081001;
input bool EnableExecution = false;
// The fast path is a quote/command heartbeat.  Complete account snapshots
// are pushed on trade transactions and periodically for recovery, rather
// than being rebuilt on every poll tick.
input int SnapshotIntervalMilliseconds = 50;
input int FullSnapshotIntervalMilliseconds = 250;
input int HttpTimeoutMilliseconds = 1000;

ulong g_last_snapshot_tick = 0;
ulong g_last_full_snapshot_tick = 0;
ulong g_last_m1_history_tick = 0;
bool g_m1_history_dirty = false;
bool g_full_snapshot_due = true;
string g_m1_bars_json = "";
int g_m1_server_utc_offset_seconds = 0;
string g_pending_receipt_body = "";
string g_last_command_id = "";
string g_last_command_phase = "";
string g_last_command_reference = "";

int OnInit()
{
   LoadCommandState();
   EventSetMillisecondTimer((int)MathMax(10, SnapshotIntervalMilliseconds));
   RefreshM1History(true);
   string health_response;
   int health_status = 0;
   if(!Http("GET", "/v1/status", "", health_response, health_status))
      PrintFormat("ATAS bridge health check failed, HTTP=%d", health_status);
   else
      Print("ATAS bridge health check succeeded");
   string startup_command;
   if(PublishFullSnapshot(startup_command) && EnableExecution && StringLen(startup_command) > 0)
      ExecuteCommandWire(startup_command);
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   // A receipt that was accepted by the terminal but not yet by the local
   // service must be retried before polling another command. The service
   // de-duplicates receipts by command ID.
   if(!FlushPendingReceipt())
      return;
   ulong now = GetTickCount();
   RefreshM1History(false);
   bool heartbeat_posted = false;
   string heartbeat_command = "";
   if(now - g_last_snapshot_tick >= (ulong)MathMax(10, SnapshotIntervalMilliseconds))
      heartbeat_posted = g_full_snapshot_due
                         || now - g_last_full_snapshot_tick >= (ulong)MathMax(100, FullSnapshotIntervalMilliseconds)
                         ? PublishFullSnapshot(heartbeat_command)
                         : PublishHeartbeat(heartbeat_command);
   // The bridge selects a command while processing this exact heartbeat or
   // full snapshot and returns it in-band.  Trade callbacks force the next
   // timer tick onto the full path so command effects are observed promptly.
   if(EnableExecution && heartbeat_posted && StringLen(heartbeat_command) > 0)
      ExecuteCommandWire(heartbeat_command);
}

void OnTradeTransaction(const MqlTradeTransaction &transaction,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
   // Trading handlers must remain fast. The next timer tick publishes a full
   // bridge-filtered snapshot; snapshots, rather than callback ordering, are
   // the authority used by the local coordinator.
   g_full_snapshot_due = true;
   g_last_snapshot_tick = 0;
}

bool PublishFullSnapshot(string &command_response)
{
   command_response = "";
   bool include_history = g_m1_history_dirty;
   string body = BuildSnapshotJson(include_history);
   string response;
   int status = 0;
   if(!Http("POST", "/v1/mt5/snapshot?poll=1", body, response, status))
   {
      PrintFormat("ATAS bridge snapshot POST failed, HTTP=%d", status);
      return false;
   }
   if(include_history)
      g_m1_history_dirty = false;
   g_last_snapshot_tick = GetTickCount();
   g_last_full_snapshot_tick = g_last_snapshot_tick;
   g_full_snapshot_due = false;
   // An old service returns JSON {"ok":true}.  It is valid for observation
   // mode but is not a command wire payload, so silently ignore it while an
   // upgraded service is being restarted.
   if(StringLen(response) > 0 && StringGetCharacter(response, 0) != '{')
      command_response = response;
   return true;
}

bool PublishHeartbeat(string &command_response)
{
   command_response = "";
   MqlTick tick = {};
   SymbolInfoTick(_Symbol, tick);
   string body = StringFormat(
      "{\"target_symbol\":\"%s\",\"pair_tag\":\"%s\",\"bridge_magic\":%I64d,\"quote\":{\"bid\":\"%s\",\"ask\":\"%s\",\"occurred_at_utc\":\"%s\"}}",
      EscapeJson(_Symbol), EscapeJson(ResolvedPairTag()), BridgeMagic, Number(tick.bid), Number(tick.ask), UtcIso(TimeGMT()));
   string response;
   int status = 0;
   if(!Http("POST", "/v1/mt5/heartbeat?poll=1", body, response, status))
   {
      PrintFormat("ATAS bridge heartbeat POST failed, HTTP=%d", status);
      return false;
   }
   g_last_snapshot_tick = GetTickCount();
   if(StringLen(response) > 0 && StringGetCharacter(response, 0) != '{')
      command_response = response;
   return true;
}

string BuildSnapshotJson(bool include_history)
{
   MqlTick tick = {};
   SymbolInfoTick(_Symbol, tick);
   double long_volume = 0.0;
   double short_volume = 0.0;
   string positions = "";
   bool first_position = true;
   bool foreign_symbol_objects_present = false;
   for(int index = PositionsTotal() - 1; index >= 0; index--)
   {
      ulong ticket = PositionGetTicket(index);
      if(ticket == 0 || PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;
      // Brokers may rewrite POSITION_COMMENT when pending orders fill or a
      // netting position is merged. Magic remains the ownership boundary for
      // positions; pending orders additionally require their AB2 leg tag.
      if((long)PositionGetInteger(POSITION_MAGIC) != BridgeMagic)
      {
         foreign_symbol_objects_present = true;
         continue;
      }
      ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      double volume = PositionGetDouble(POSITION_VOLUME);
      string side = type == POSITION_TYPE_BUY ? "long" : "short";
      if(type == POSITION_TYPE_BUY)
         long_volume += volume;
      else
         short_volume += volume;
      if(!first_position)
         positions += ",";
      positions += StringFormat("{\"ticket\":\"%I64u\",\"side\":\"%s\",\"quantity\":\"%s\",\"stop_loss\":\"%s\",\"take_profit\":\"%s\"}",
                                ticket, side, Number(volume), Number(PositionGetDouble(POSITION_SL)),
                                Number(PositionGetDouble(POSITION_TP)));
      first_position = false;
   }

   string orders = "";
   bool first_order = true;
   for(int index = OrdersTotal() - 1; index >= 0; index--)
   {
      ulong ticket = OrderGetTicket(index);
      if(ticket == 0 || OrderGetString(ORDER_SYMBOL) != _Symbol)
         continue;
      string comment = OrderGetString(ORDER_COMMENT);
      if((long)OrderGetInteger(ORDER_MAGIC) != BridgeMagic || !IsBridgeComment(comment))
      {
         foreign_symbol_objects_present = true;
         continue;
      }
      ENUM_ORDER_TYPE type = (ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
      string side = (type == ORDER_TYPE_BUY_LIMIT || type == ORDER_TYPE_BUY_STOP || type == ORDER_TYPE_BUY_STOP_LIMIT)
                    ? "buy" : "sell";
      string order_type = (type == ORDER_TYPE_BUY_LIMIT || type == ORDER_TYPE_SELL_LIMIT ||
                           type == ORDER_TYPE_BUY_STOP_LIMIT || type == ORDER_TYPE_SELL_STOP_LIMIT)
                          ? "limit" : "stop";
      string tag = ExtractBridgeTag(comment);
      if(!first_order)
         orders += ",";
      orders += StringFormat("{\"ticket\":\"%I64u\",\"side\":\"%s\",\"order_type\":\"%s\",\"quantity\":\"%s\",\"price\":\"%s\",\"trigger_price\":\"%s\",\"bridge_tag\":\"%s\"}",
                             ticket, side, order_type, Number(OrderGetDouble(ORDER_VOLUME_CURRENT)),
                             Number(OrderGetDouble(ORDER_PRICE_OPEN)), Number(OrderGetDouble(ORDER_PRICE_STOPLIMIT)),
                             EscapeJson(tag));
      first_order = false;
   }

   string account_model = AccountInfoInteger(ACCOUNT_MARGIN_MODE) == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
                          ? "hedging" : "netting";
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   string history = include_history
                    ? StringFormat("\"m1_bars\":[%s],\"m1_server_utc_offset_seconds\":%d,", g_m1_bars_json, g_m1_server_utc_offset_seconds)
                    : "";
   return StringFormat(
      "{\"target_symbol\":\"%s\",\"pair_tag\":\"%s\",\"bridge_magic\":%I64d,\"account_model\":\"%s\",\"snapshot_complete\":true,%s"
      "\"long_quantity\":\"%s\",\"short_quantity\":\"%s\",\"quote\":{\"bid\":\"%s\",\"ask\":\"%s\",\"occurred_at_utc\":\"%s\"},"
      "\"symbol_spec\":{\"tick_size\":\"%s\",\"volume_min\":\"%s\",\"volume_max\":\"%s\",\"volume_step\":\"%s\",\"stops_level\":\"%s\",\"freeze_level\":\"%s\"},"
      "\"pending_orders\":[%s],\"positions\":[%s],\"foreign_symbol_objects_present\":%s}",
      EscapeJson(_Symbol), EscapeJson(ResolvedPairTag()), BridgeMagic, account_model, history, Number(long_volume), Number(short_volume),
      Number(tick.bid), Number(tick.ask), UtcIso(TimeGMT()),
      Number(SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE)), Number(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN)),
      Number(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX)), Number(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP)),
      Number((double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * point),
      Number((double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_FREEZE_LEVEL) * point), orders, positions,
      foreign_symbol_objects_present ? "true" : "false");
}

void RefreshM1History(bool force)
{
   ulong now = GetTickCount();
   if(!force && now - g_last_m1_history_tick < 60000)
      return;
   g_last_m1_history_tick = now;
   MqlRates rates[];
   int copied = CopyRates(_Symbol, PERIOD_M1, 1, 240, rates);
   // The bridge's configured minimum is 170 matching closed M1 samples.  A
   // normal GC maintenance halt can leave roughly 175 bars in the latest
   // four-hour window, which is still sufficient for calibration.
   if(copied < 170)
   {
      PrintFormat("ATAS bridge M1 history unavailable or incomplete: copied=%d", copied);
      return;
   }
   g_m1_server_utc_offset_seconds = (int)(TimeCurrent() - TimeGMT());
   string bars = "";
   for(int index = 0; index < copied; index++)
   {
      if(index > 0)
         bars += ",";
      datetime utc_time = rates[index].time - g_m1_server_utc_offset_seconds;
      bars += StringFormat("{\"time_utc\":\"%s\",\"close\":\"%s\"}", UtcIso(utc_time), Number(rates[index].close));
   }
   g_m1_bars_json = bars;
   g_m1_history_dirty = true;
}

void PollAndExecuteOneCommand()
{
   string response;
   int status = 0;
   // Snapshot and receipt POSTs are already the proven transport path in
   // MT5. Use the same path for command polling; some terminal builds return
   // a non-standard status for a non-empty GET response on loopback.
   if(!Http("POST", "/v1/mt5/command", "{}", response, status) || StringLen(response) == 0)
      return;
   ExecuteCommandWire(response);
}

void ExecuteCommandWire(string response)
{
   string fields[];
   int count = StringSplit(response, 9, fields);
   if(count != 11)
   {
      Print("ATAS bridge command has invalid TSV shape");
      return;
   }
   if(fields[0] == g_last_command_id && StringLen(g_last_command_phase) > 0)
   {
      if(g_last_command_phase == "executing")
      {
         // Terminal process stopped after durable intent but before a local
         // outcome was recorded. Do not risk sending the trade twice.
         SaveCommandState(fields[0], "unknown", "");
         SendReceipt(fields[0], "unknown", "", "EA restarted after request dispatch; target effect is unknown");
      }
      else
      {
         SendReceipt(fields[0], g_last_command_phase, g_last_command_reference, "replayed durable EA receipt");
      }
      return;
   }
   ExecuteCommand(fields);
}

void ExecuteCommand(string &fields[])
{
   string command_id = fields[0];
   string command_type = fields[1];
   string target_symbol = fields[2];
   if(target_symbol != _Symbol)
   {
      CompleteCommand(command_id, "failed", "", "command symbol does not match EA chart");
      return;
   }
   MqlTradeRequest request = {};
   MqlTradeResult result = {};
   request.symbol = _Symbol;
   request.magic = BridgeMagic;
   request.volume = StringToDouble(fields[5]);
   request.price = StringToDouble(fields[6]);
   request.stoplimit = StringToDouble(fields[7]);

   if(command_type == "create_pending")
   {
      request.action = TRADE_ACTION_PENDING;
      request.type = PendingType(fields[3], fields[4]);
      request.type_filling = ORDER_FILLING_RETURN;
      request.comment = "AB2:" + fields[10];
   }
   else if(command_type == "cancel_pending")
   {
      request.action = TRADE_ACTION_REMOVE;
      request.order = (ulong)StringToInteger(fields[9]);
   }
   else if(command_type == "execute_market")
   {
      request.action = TRADE_ACTION_DEAL;
      request.type = fields[3] == "buy" ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      request.type_filling = ResolveFillingMode();
      request.price = fields[3] == "buy" ? SymbolInfoDouble(_Symbol, SYMBOL_ASK) : SymbolInfoDouble(_Symbol, SYMBOL_BID);
      request.comment = "AB2:reconcile";
   }
   else if(command_type == "close_position")
   {
      request.action = TRADE_ACTION_DEAL;
      request.position = (ulong)StringToInteger(fields[8]);
      request.type = fields[3] == "buy" ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      request.type_filling = ResolveFillingMode();
      request.price = fields[3] == "buy" ? SymbolInfoDouble(_Symbol, SYMBOL_ASK) : SymbolInfoDouble(_Symbol, SYMBOL_BID);
      request.comment = "AB2:close";
   }
   else if(command_type == "modify_position_protection")
   {
      request.action = TRADE_ACTION_SLTP;
      request.position = (ulong)StringToInteger(fields[8]);
      request.sl = StringToDouble(fields[6]);
      request.tp = StringToDouble(fields[7]);
   }
   else
   {
      CompleteCommand(command_id, "failed", "", "unsupported command type");
      return;
   }

   if(!SaveCommandState(command_id, "executing", ""))
   {
      SendReceipt(command_id, "failed", "", "cannot persist EA command state before dispatch");
      return;
   }
   // A pending-order create must return its broker-assigned ticket.  The old
   // asynchronous call commonly returned order=0, leaving the bridge unable
   // to cancel an ATAS order which was withdrawn before the next full MT5
   // snapshot.  OrderSend waits only for this command's broker response and
   // preserves the low-frequency full snapshot as confirmation/audit.
   ResetLastError();
   if(!OrderSend(request, result))
   {
      CompleteCommand(command_id, "failed", "", StringFormat("OrderSend failed, error=%d", GetLastError()));
      return;
   }
   if(!IsAcceptedTradeRetcode(result.retcode))
   {
      CompleteCommand(command_id, "failed", "", StringFormat("MT5 rejected command, retcode=%u: %s", result.retcode, result.comment));
      return;
   }
   string target_reference = "";
   if(command_type == "create_pending")
      target_reference = StringFormat("%I64u", result.order);
   else if(command_type == "cancel_pending")
      target_reference = fields[9];
   else if(command_type == "close_position" || command_type == "modify_position_protection")
      target_reference = fields[8];
   CompleteCommand(command_id, "accepted", target_reference, StringFormat("MT5 accepted request, retcode=%u", result.retcode));
}

bool IsAcceptedTradeRetcode(uint retcode)
{
   return retcode == TRADE_RETCODE_DONE ||
          retcode == TRADE_RETCODE_PLACED ||
          retcode == TRADE_RETCODE_DONE_PARTIAL;
}

ENUM_ORDER_TYPE PendingType(string side, string order_type)
{
   // The Python safety gate has already validated semantic side/price. The
   // MT5 request only needs the resulting order class. A non-empty stoplimit
   // is not used by v0.1, so StopLimit types are deliberately excluded.
   if(side == "buy")
      return order_type == "limit" ? ORDER_TYPE_BUY_LIMIT : ORDER_TYPE_BUY_STOP;
   return order_type == "limit" ? ORDER_TYPE_SELL_LIMIT : ORDER_TYPE_SELL_STOP;
}

ENUM_ORDER_TYPE_FILLING ResolveFillingMode()
{
   int modes = (int)SymbolInfoInteger(_Symbol, SYMBOL_FILLING_MODE);
   if((modes & ORDER_FILLING_FOK) == ORDER_FILLING_FOK)
      return ORDER_FILLING_FOK;
   if((modes & ORDER_FILLING_IOC) == ORDER_FILLING_IOC)
      return ORDER_FILLING_IOC;
   return ORDER_FILLING_RETURN;
}

void SendReceipt(string command_id, string status_text, string target_reference, string message)
{
   g_pending_receipt_body = StringFormat("{\"command_id\":\"%s\",\"pair_tag\":\"%s\",\"status\":\"%s\",\"target_reference\":\"%s\",\"message\":\"%s\"}",
                                         EscapeJson(command_id), EscapeJson(ResolvedPairTag()), EscapeJson(status_text), EscapeJson(target_reference), EscapeJson(message));
   FlushPendingReceipt();
}

bool FlushPendingReceipt()
{
   if(StringLen(g_pending_receipt_body) == 0)
      return true;
   string response;
   int status = 0;
   if(!Http("POST", "/v1/mt5/receipt", g_pending_receipt_body, response, status))
   {
      PrintFormat("ATAS bridge receipt POST failed, HTTP=%d", status);
      return false;
   }
   g_pending_receipt_body = "";
   return true;
}

void CompleteCommand(string command_id, string phase, string target_reference, string message)
{
   if(!SaveCommandState(command_id, phase, target_reference))
      Print("ATAS bridge could not persist EA command result");
   SendReceipt(command_id, phase, target_reference, message);
}

void LoadCommandState()
{
   int handle = FileOpen(CommandStateFileName(), FILE_READ | FILE_TXT | FILE_COMMON | FILE_ANSI);
   if(handle == INVALID_HANDLE)
      return;
   g_last_command_id = FileReadString(handle);
   g_last_command_phase = FileReadString(handle);
   g_last_command_reference = FileReadString(handle);
   FileClose(handle);
}

bool SaveCommandState(string command_id, string phase, string target_reference)
{
   int handle = FileOpen(CommandStateFileName(), FILE_WRITE | FILE_TXT | FILE_COMMON | FILE_ANSI);
   if(handle == INVALID_HANDLE)
   {
      PrintFormat("ATAS bridge state file error=%d", GetLastError());
      return false;
   }
   FileWrite(handle, command_id);
   FileWrite(handle, phase);
   FileWrite(handle, target_reference);
   FileClose(handle);
   g_last_command_id = command_id;
   g_last_command_phase = phase;
   g_last_command_reference = target_reference;
   return true;
}

string CommandStateFileName()
{
   return StringFormat("AtasMt5BridgeEA-%I64d-%s-%s.state", BridgeMagic, StateComponent(_Symbol), StateComponent(ResolvedPairTag()));
}

string ResolvedPairTag()
{
   string configured = PairTag;
   StringTrimLeft(configured);
   StringTrimRight(configured);
   if(StringLen(configured) > 0)
      return configured;

   string symbol = _Symbol;
   StringToUpper(symbol);
   if(StringFind(symbol, "XAUUSD") >= 0)
      return "gc_xauusd";
   if(StringFind(symbol, "USTEC") >= 0)
      return "nq_ustec";
   return "";
}

string StateComponent(string value)
{
   StringReplace(value, "\\", "_");
   StringReplace(value, "/", "_");
   StringReplace(value, ":", "_");
   StringReplace(value, "*", "_");
   StringReplace(value, "?", "_");
   StringReplace(value, "\"", "_");
   StringReplace(value, "<", "_");
   StringReplace(value, ">", "_");
   StringReplace(value, "|", "_");
   if(StringLen(value) == 0)
      return "default";
   return value;
}

bool Http(string method, string endpoint, string body, string &response, int &status)
{
   string headers = "Content-Type: application/json\r\n";
   char data[];
   char result[];
   string response_headers;
   // The MQL5 WebRequest implementation is most reliable when it receives
   // the native StringToCharArray buffer, including its terminating NUL.
   // The loopback service strips that one transport terminator before JSON
   // parsing, rather than resizing MQL's request buffer on the EA side.
   StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
   ResetLastError();
   status = WebRequest(method, BridgeBaseUrl + endpoint, headers, HttpTimeoutMilliseconds, data, result, response_headers);
   if(status == -1)
   {
      PrintFormat("ATAS bridge WebRequest error=%d. Add %s to allowed URLs.", GetLastError(), BridgeBaseUrl);
      return false;
   }
   response = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   if(status < 200 || status >= 300)
      PrintFormat("ATAS bridge HTTP status=%d endpoint=%s response=%s", status, endpoint, response);
   return status >= 200 && status < 300;
}

string ExtractBridgeTag(string comment)
{
   if(StringFind(comment, "AB2:") != 0)
      return "";
   return StringSubstr(comment, 4);
}

bool IsBridgeComment(string comment)
{
   return StringFind(comment, "AB2:") == 0;
}

string EscapeJson(string value)
{
   StringReplace(value, "\\", "\\\\");
   StringReplace(value, "\"", "\\\"");
   StringReplace(value, "\n", "\\n");
   StringReplace(value, "\r", "\\r");
   return value;
}

string Number(double value)
{
   return DoubleToString(value, (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS));
}

string UtcIso(datetime value)
{
   MqlDateTime parts = {};
   TimeToStruct(value, parts);
   return StringFormat("%04d-%02d-%02dT%02d:%02d:%02d+00:00", parts.year, parts.mon, parts.day, parts.hour, parts.min, parts.sec);
}
