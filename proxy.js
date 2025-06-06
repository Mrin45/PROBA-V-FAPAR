const express = require('express');
const axios = require('axios');
const NodeCache = require('node-cache');

const app = express();
const cache = new NodeCache({ stdTTL: 86400 }); // 24 hours

app.use((req, res, next) => {
    res.setHeader('Access-Control-Allow-Origin', '*');
    next();
});

app.get('/proxy', async (req, res) => {
    try {
        const url = req.query.url;
        if (!url) {
            return res.status(400).send('URL parameter is required');
        }
        const cacheKey = url;
        const cached = cache.get(cacheKey);
        if (cached) {
            res.setHeader('Content-Type', 'text/html');
            return res.send(cached);
        }
        const response = await axios.get(url, {
            responseType: 'stream',
            timeout: 10000
        });
        const chunks = [];
        response.data.on('data', chunk => chunks.push(chunk));
        response.data.on('end', () => {
            const data = Buffer.concat(chunks);
            cache.set(cacheKey, data, 86400);
            res.setHeader('Content-Type', response.headers['content-type'] || 'text/html');
            res.send(data);
        });
        response.data.on('error', e => {
            console.error(`Stream error: ${e.message}`);
            res.status(500).send(`Stream error: ${e.message}`);
        });
    } catch (error) {
        console.error(`Proxy error: ${error.message}`);
        res.status(500).send(`Proxy error: ${error.message}`);
    }
});

app.listen(3001, () => console.log('Proxy running on port 3001'));