using System;
using System.Collections.Concurrent;
using System.IO;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;

namespace AtasMt5ProbeStrategy
{
    internal sealed class ProbeWebSocketClient : IDisposable
    {
        private readonly Uri _serverUri;
        private readonly SemaphoreSlim _connectionLock = new SemaphoreSlim(1, 1);
        private readonly SemaphoreSlim _sendLock = new SemaphoreSlim(1, 1);
        private readonly CancellationTokenSource _cts = new CancellationTokenSource();
        private readonly ConcurrentDictionary<long, TaskCompletionSource<ProbeAck?>> _pendingAcks =
            new ConcurrentDictionary<long, TaskCompletionSource<ProbeAck?>>();
        private ClientWebSocket? _socket;
        private Task? _receiveLoop;
        private bool _disposed;

        public ProbeWebSocketClient(string serverUrl)
        {
            _serverUri = new Uri(string.IsNullOrWhiteSpace(serverUrl)
                ? "ws://127.0.0.1:8767"
                : serverUrl.Trim());
        }

        public bool IsConnected => !_disposed && _socket?.State == WebSocketState.Open;

        public async Task<bool> EnsureConnectedAsync()
        {
            if (IsConnected)
                return true;

            await _connectionLock.WaitAsync();
            try
            {
                if (IsConnected)
                    return true;

                CloseSocket();
                var socket = new ClientWebSocket();
                await socket.ConnectAsync(_serverUri, _cts.Token);
                _socket = socket;
                _receiveLoop = Task.Run(ReceiveLoopAsync);
                return true;
            }
            catch
            {
                CloseSocket();
                return false;
            }
            finally
            {
                _connectionLock.Release();
            }
        }

        public async Task<ProbeAck?> SendAsync(
            ProbeEnvelope message,
            bool waitForAcknowledgement,
            int acknowledgementTimeoutMilliseconds)
        {
            if (!await EnsureConnectedAsync() || _socket == null)
                return null;

            TaskCompletionSource<ProbeAck?>? completion = null;
            if (waitForAcknowledgement)
            {
                completion = new TaskCompletionSource<ProbeAck?>(TaskCreationOptions.RunContinuationsAsynchronously);
                if (!_pendingAcks.TryAdd(message.EventId, completion))
                    throw new InvalidOperationException($"Duplicate probe event id: {message.EventId}");
            }

            try
            {
                var json = JsonConvert.SerializeObject(message);
                var bytes = Encoding.UTF8.GetBytes(json);
                await _sendLock.WaitAsync(_cts.Token);
                try
                {
                    if (_socket?.State != WebSocketState.Open)
                        return null;

                    await _socket.SendAsync(
                        new ArraySegment<byte>(bytes),
                        WebSocketMessageType.Text,
                        true,
                        _cts.Token);
                }
                finally
                {
                    _sendLock.Release();
                }

                if (completion == null)
                    return null;

                var timeout = Task.Delay(Math.Max(1, acknowledgementTimeoutMilliseconds), _cts.Token);
                var completed = await Task.WhenAny(completion.Task, timeout);
                return completed == completion.Task ? await completion.Task : null;
            }
            catch
            {
                CloseSocket();
                return null;
            }
            finally
            {
                if (completion != null)
                    _pendingAcks.TryRemove(message.EventId, out _);
            }
        }

        private async Task ReceiveLoopAsync()
        {
            try
            {
                while (!_cts.IsCancellationRequested && _socket?.State == WebSocketState.Open)
                {
                    var json = await ReceiveTextAsync(_socket, _cts.Token);
                    if (json == null)
                        break;

                    var ack = JsonConvert.DeserializeObject<ProbeAck>(json);
                    if (ack?.Type == "probe_ack")
                    {
                        if (_pendingAcks.TryRemove(ack.EventId, out var completion))
                            completion.TrySetResult(ack);
                    }
                }
            }
            catch
            {
                // The next event will reconnect. Probe data must never interrupt ATAS callbacks.
            }
            finally
            {
                foreach (var pending in _pendingAcks)
                    pending.Value.TrySetResult(null);
            }
        }

        private static async Task<string?> ReceiveTextAsync(ClientWebSocket socket, CancellationToken token)
        {
            var buffer = new byte[4096];
            using (var stream = new MemoryStream())
            {
                WebSocketReceiveResult result;
                do
                {
                    result = await socket.ReceiveAsync(new ArraySegment<byte>(buffer), token);
                    if (result.MessageType == WebSocketMessageType.Close)
                        return null;

                    stream.Write(buffer, 0, result.Count);
                }
                while (!result.EndOfMessage);

                return Encoding.UTF8.GetString(stream.ToArray());
            }
        }

        private void CloseSocket()
        {
            try
            {
                _socket?.Dispose();
            }
            catch
            {
                // Best-effort cleanup after a broken local connection.
            }
            finally
            {
                _socket = null;
            }
        }

        public void Dispose()
        {
            if (_disposed)
                return;

            _disposed = true;
            _cts.Cancel();
            CloseSocket();
            _connectionLock.Dispose();
            _sendLock.Dispose();
            _cts.Dispose();
        }
    }
}
