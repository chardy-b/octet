const refresh = async () => {
	const display = document.getElementById("now-playing");
	const connection = document.getElementById("connection");
	const cover = document.getElementById("cover");
	try {
		const response = await fetch("/api/now-playing", { cache: "no-store", signal: AbortSignal.timeout(5000) });
		if (!response.ok) {
			throw new Error("Unavailable");
		}
		const song = await response.json();
		const playing = song.state === "playing";
		const artwork = playing && /^\/api\/artwork\/[a-f0-9]{64}$/u.test(song.artwork || "") ? song.artwork : null;
		cover.hidden = !artwork;
		if (artwork) {
			if (cover.getAttribute("src") !== artwork) {
				cover.src = artwork;
			}
			cover.alt = song.album ? `${song.album} album cover` : "Album cover";
			cover.onerror = () => { cover.hidden = true; };
		} else {
			cover.removeAttribute("src");
		}
		document.getElementById("title").textContent = playing ? song.title || song.source_label : "";
		document.getElementById("artist").textContent = playing ? song.artist : "";
		document.getElementById("album").textContent = playing ? song.album : "";
		document.getElementById("state").textContent = playing ? `${song.source_label} · Playing${song.title ? "" : " · Song details unavailable"}` : "";
		display.hidden = !playing;
		connection.hidden = true;
	} catch {
		display.hidden = true;
		cover.hidden = true;
		cover.removeAttribute("src");
		connection.textContent = "Octet unavailable";
		connection.hidden = false;
		for (const id of ["title", "artist", "album", "state"]) {
			document.getElementById(id).textContent = "";
		}
	}
	setTimeout(refresh, 3000);
};
refresh();
