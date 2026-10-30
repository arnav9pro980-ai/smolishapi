const express = require('express');
const cors = require('cors');

const app = express();
const PORT = process.env.PORT || 3000;

app.use(cors());
app.use(express.json());

// In-memory data store for rooms and signaling messages
const rooms = new Map();

// Helper to cleanup stale rooms (inactive for > 30 minutes)
setInterval(() => {
    const now = Date.now();
    for (const [roomId, room] of rooms.entries()) {
        if (now - room.lastActive > 30 * 60 * 1000) {
            rooms.delete(roomId);
        }
    }
}, 5 * 60 * 1000);

// Health check endpoint
app.get('/', (req, res) => {
    res.json({ status: 'CallSpace signaling server is running', activeRooms: rooms.size });
});

// Create a new room
app.post('/rooms', (req, res) => {
    const roomId = Math.random().toString(36).substring(2, 8).toUpperCase();
    rooms.set(roomId, {
        participants: [],
        messages: [],
        lastActive: Date.now()
    });
    res.json({ id: roomId });
});

// Get room details
app.get('/rooms/:id', (req, res) => {
    const room = rooms.get(req.params.id);
    if (!room) {
        return res.status(404).json({ error: 'Room not found' });
    }
    room.lastActive = Date.now();
    res.json({ participants: room.participants });
});

// Join a room
app.post('/rooms/:id/join', (req, res) => {
    const { peerId } = req.body;
    const room = rooms.get(req.params.id);
    
    if (!room) {
        return res.status(404).json({ error: 'Room not found' });
    }
    
    if (!peerId) {
        return res.status(400).json({ error: 'Missing peerId' });
    }

    if (!room.participants.includes(peerId)) {
        if (room.participants.length >= 2) {
            return res.status(403).json({ error: 'Room is full (max 2 participants)' });
        }
        room.participants.push(peerId);
    }
    
    room.lastActive = Date.now();
    res.json({ success: true, participants: room.participants });
});

// Leave a room
app.post('/rooms/:id/leave', (req, res) => {
    const { peerId } = req.body;
    const room = rooms.get(req.params.id);
    
    if (room && peerId) {
        room.participants = room.participants.filter(id => id !== peerId);
        room.lastActive = Date.now();
        if (room.participants.length === 0) {
            rooms.delete(req.params.id);
        }
    }
    
    res.json({ success: true });
});

// Send a WebRTC signaling message (offer, answer, candidate)
app.post('/rooms/:id/signal', (req, res) => {
    const { from, to, data } = req.body;
    const room = rooms.get(req.params.id);
    
    if (!room) {
        return res.status(404).json({ error: 'Room not found' });
    }
    
    if (!from || !to || !data) {
        return res.status(400).json({ error: 'Invalid signal payload' });
    }

   const messageId = Math.random().toString(36).substring(2);
    const message = {
        id: messageId,
        from,
        to,
        data,
        timestamp: Date.now()
    };
    
    room.messages.push(message);
    room.lastActive = Date.now();
    
    // Keep only last 50 messages to prevent memory bloat
    if (room.messages.length > 50) {
        room.messages.shift();
    }
    
    res.json({ success: true, messageId });
});

// Poll messages for a specific peer
app.get('/rooms/:id/messages', (req, res) => {
    const roomId = req.params.id;
    const peerId = req.query.peerId;
    const room = rooms.get(roomId);
    
    if (!room) {
        return res.status(404).json({ error: 'Room not found' });
    }
    
    room.lastActive = Date.now();
    
    // Return messages intended for this peer
    const peerMessages = room.messages.filter(m => m.to === peerId);
    res.json({ messages: peerMessages });
});

app.listen(PORT, () => {
    console.log(`CallSpace signaling server running on port ${PORT}`);
});
