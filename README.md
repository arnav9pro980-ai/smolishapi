# CallSpace Signaling Server (Node.js)

This is the Node.js Express backend signaling server for **CallSpace**, designed to coordinate WebRTC peer-to-peer video calls and handle room creation, joining, and signaling message polling.

## Local Development

1. Install dependencies:
   ```bash
   npm install
   ```
2. Start the server:
   ```bash
   npm start
   ```
   The server will run on `http://localhost:3000`.

---

## Deployment on Render

Render does not require complex configuration for Node.js apps. Follow these simple steps:

1. **Push to GitHub**: Push this `callspace-server` folder or repository to your GitHub account.
2. **Create a New Web Service on Render**:
   - Go to your [Render Dashboard](https://dashboard.render.com/).
   - Click **New** -> **Web Service**.
   - Connect your GitHub repository.
3. **Configure Service Settings**:
   - **Name**: `callspace-signaling-server` (or any name you like)
   - **Environment**: `Node`
   - **Build Command**: `npm install`
   - **Start Command**: `npm start`
   - **Instance Type**: **Free**
4. **Deploy**:
   - Click **Create Web Service**.
   - Once deployment completes, Render will provide your backend URL (e.g., `https://callspace-server-xyz.onrender.com`).

---

## Connecting Frontend to Render

In your frontend `index.html`, update the `API` constant to point to your Render backend URL:

```javascript
const API = "https://your-render-app-name.onrender.com";
```
