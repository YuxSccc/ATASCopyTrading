using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace AtasMt5ProbeStrategy
{
    internal sealed class ProbeEnvelope
    {
        [JsonProperty("type")]
        public string Type { get; set; } = "probe_event";

        [JsonProperty("protocol_version")]
        public int ProtocolVersion { get; set; } = 1;

        [JsonProperty("session_id")]
        public string SessionId { get; set; } = "";

        [JsonProperty("event_id")]
        public long EventId { get; set; }

        // Queue sequencing is local-only. A queued quote may be discarded to
        // let a later trading event pass, but discarded messages must never
        // consume the wire event sequence used by the bridge state machine.
        [JsonIgnore]
        public long QueueSequence { get; set; }

        [JsonProperty("event_type")]
        public string EventType { get; set; } = "";

        [JsonProperty("source_symbol")]
        public string SourceSymbol { get; set; } = "";

        // Stable logical pair identifier.  It is deliberately independent of
        // the broker/connector symbol text (which may contain aliases or
        // contract months) and lets several chart strategies share one
        // authenticated WebSocket.
        [JsonProperty("pair_tag")]
        public string PairTag { get; set; } = "";

        // Portfolio/connector identity is diagnostic and is used by the
        // bridge to recognize an account switch.  It is deliberately not a
        // credential; ATAS supplies only its display identity.
        [JsonProperty("source_account")]
        public string SourceAccount { get; set; } = "";

        [JsonProperty("atas_timestamp_utc")]
        public string AtasTimestampUtc { get; set; } = "";

        [JsonProperty("local_monotonic_ticks")]
        public long LocalMonotonicTicks { get; set; }

        [JsonProperty("local_stopwatch_frequency")]
        public long LocalStopwatchFrequency { get; set; }

        [JsonProperty("delivery_mode")]
        public string DeliveryMode { get; set; } = "async";

        [JsonProperty("data")]
        public object Data { get; set; } = new object();
    }

    internal sealed class ProbeAck
    {
        [JsonProperty("type")]
        public string Type { get; set; } = "";

        [JsonProperty("session_id")]
        public string SessionId { get; set; } = "";

        [JsonProperty("event_id")]
        public long EventId { get; set; }

        [JsonProperty("server_received_utc")]
        public string ServerReceivedUtc { get; set; } = "";

        [JsonProperty("server_received_monotonic_ns")]
        public long ServerReceivedMonotonicNs { get; set; }

        [JsonProperty("test_command")]
        public ProbeTestCommand? TestCommand { get; set; }

        [JsonProperty("bridge_status")]
        public ProbeBridgeStatus? BridgeStatus { get; set; }
    }

    internal sealed class ProbeBridgeStatus
    {
        [JsonProperty("state")]
        public string State { get; set; } = "";

        [JsonProperty("message")]
        public string Message { get; set; } = "";

        [JsonProperty("reconciliation_required")]
        public bool ReconciliationRequired { get; set; }
    }

    internal sealed class ProbeTestCommand
    {
        [JsonProperty("command_id")]
        public string CommandId { get; set; } = "";

        [JsonProperty("run_id")]
        public string RunId { get; set; } = "";

        [JsonProperty("command")]
        public string Command { get; set; } = "";

        [JsonProperty("source_order_id")]
        public string SourceOrderId { get; set; } = "";

        [JsonProperty("parameters")]
        public JObject Parameters { get; set; } = new JObject();
    }
}
