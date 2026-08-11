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

        [JsonProperty("event_type")]
        public string EventType { get; set; } = "";

        [JsonProperty("source_symbol")]
        public string SourceSymbol { get; set; } = "";

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
