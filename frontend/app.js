let currentUser = null;

// -------------------- API HELPERS --------------------

async function sendRequest(url, data) {
    const response = await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify(data)
    });

    const result = await response.json();

    if (!response.ok) {
        throw new Error(result.error || "Something went wrong.");
    }

    return result;
}

async function getRequest(url) {
    const response = await fetch(url, {
        method: "GET",
        credentials: "same-origin"
    });

    const result = await response.json();

    if (!response.ok) {
        throw new Error(result.error || "Something went wrong.");
    }

    return result;
}

function getFormData(form) {
    return Object.fromEntries(new FormData(form).entries());
}

function showMessage(id, message, isError = false) {
    const element = document.getElementById(id);

    if (!element) return;

    element.textContent = message;
    element.style.color = isError ? "#c0392b" : "#238b73";
}

function escapeHTML(value) {
    return String(value ?? "").replace(/[&<>"']/g, character => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
    })[character]);
}

function isUnknownFare(value) {
    return value === null || value === undefined || value === "";
}

function formatFare(value, status = "") {
    if (isUnknownFare(value)) {
        return "Fare depends on the auto driver's quotation.";
    }

    const amount = Number(value);
    if (!Number.isFinite(amount) || amount <= 0) {
        return "Fare not available";
    }

    const label = status ? ` (${escapeHTML(status)})` : "";
    return `₹${amount.toFixed(2)}${label}`;
}

function formatFareShare(value, vehicleType = "") {
    if (isUnknownFare(value)) {
        return vehicleType === "Auto"
            ? "Fare depends on the auto driver's quotation."
            : "Fare share not available";
    }

    const amount = Number(value);
    if (!Number.isFinite(amount) || amount < 0) {
        return "Fare share not available";
    }

    return `₹${amount.toFixed(2)}`;
}

function validRollNumber(value) {
    return /^[A-Za-z0-9]{8}$/.test(value || "");
}

function validPhoneNumber(value) {
    return /^[0-9]{10}$/.test(value || "");
}

function validRoute(pickup, destination) {
    const locations = [
        "IIT Patna",
        "Dhanapur",
        "Patna Airport",
        "Patna Railway Station",
        "Patna Junction Railway Station",
        "Ara Railway Station",
        "Bihta Railway Station"
    ];

    return locations.includes(pickup) &&
        locations.includes(destination) &&
        pickup !== destination;
}

// -------------------- VEHICLE CONFIGURATION --------------------

const VEHICLE_CAPACITIES = {
    "Uber Go": [4],
    "Go Non AC": [4],
    "Premier": [4],
    "Uber XL": [6],
    "Auto": [4, 5],
    "Other 4 Seater Car": [4],
    "Other 6 Seater Car": [6]
};

// -------------------- AUTHENTICATION TABS --------------------

function showAuthForm(type) {
    const loginForm = document.getElementById("loginForm");
    const registerForm = document.getElementById("registerForm");

    const loginButton = document.getElementById("showLoginBtn");
    const registerButton = document.getElementById("showRegisterBtn");

    const isLogin = type === "login";

    loginForm.classList.toggle("hidden", !isLogin);
    registerForm.classList.toggle("hidden", isLogin);

    loginButton.classList.toggle("active", isLogin);
    registerButton.classList.toggle("active", !isLogin);

    showMessage("loginMessage", "");
    showMessage("registerMessage", "");
}

document.getElementById("showLoginBtn").addEventListener("click", () => {
    showAuthForm("login");
});

document.getElementById("showRegisterBtn").addEventListener("click", () => {
    showAuthForm("register");
});

document.getElementById("goToRegister").addEventListener("click", () => {
    showAuthForm("register");
});

document.getElementById("goToLogin").addEventListener("click", () => {
    showAuthForm("login");
});

// -------------------- AUTHENTICATED APP --------------------

function fillStudentDetails() {
    if (!currentUser) return;

    const name = currentUser.name || "";
    const roll = currentUser.roll_number || "";
    const contact = currentUser.contact_number || "";

    const fields = [
        ["findName", name],
        ["findRoll", roll],
        ["findContact", contact],
        ["offerName", name],
        ["offerRoll", roll],
        ["offerContact", contact]
    ];

    fields.forEach(([id, value]) => {
        const field = document.getElementById(id);
        if (field) field.value = value;
    });

    document.getElementById("profileName").textContent =
        name || "-";

    document.getElementById("profileRoll").textContent =
        roll || "-";

    document.getElementById("profileContact").textContent =
        contact || "Not added";

    document.getElementById("profileGender").textContent =
        currentUser.gender || "Not added";

    const groupGender = document.getElementById("groupAccountGender");
    if (groupGender) groupGender.textContent = currentUser.gender || "Not added";

    const initial = name.trim().charAt(0).toUpperCase() || "?";

    document.getElementById("profileInitial").textContent = initial;
    document.getElementById("profileAvatar").textContent = initial;
}

function showApplication() {
    document.getElementById("authSection").classList.add("hidden");
    document.getElementById("appMain").classList.remove("hidden");
    document.getElementById("profileBtn").classList.remove("hidden");
    document.getElementById("mainNav").classList.remove("hidden");

    fillStudentDetails();
    showSection("home");
}

function showAuthentication() {
    document.getElementById("authSection").classList.remove("hidden");
    document.getElementById("appMain").classList.add("hidden");
    document.getElementById("profileBtn").classList.add("hidden");
    document.getElementById("mainNav").classList.add("hidden");
    document.getElementById("profilePanel").classList.add("hidden");

    currentUser = null;
}

// -------------------- REGISTER --------------------

document.getElementById("registerForm").addEventListener(
    "submit",
    async function(event) {
        event.preventDefault();

        const data = getFormData(this);

        if (!validRollNumber(data.roll_number)) {
            showMessage(
                "registerMessage",
                "Invalid roll number. Roll number must contain exactly 8 alphanumeric characters.",
                true
            );
            return;
        }

        if (!validPhoneNumber(data.contact_number)) {
            showMessage(
                "registerMessage",
                "Invalid phone number. Please enter a valid 10-digit phone number.",
                true
            );
            return;
        }

        if (data.password !== data.confirm_password) {
            showMessage(
                "registerMessage",
                "Passwords do not match.",
                true
            );
            return;
        }

        if (data.password.length < 8) {
            showMessage(
                "registerMessage",
                "Password must contain at least 8 characters.",
                true
            );
            return;
        }

        const button = this.querySelector('button[type="submit"]');
        button.disabled = true;
        button.textContent = "Creating account...";

        try {
            const result = await sendRequest("/api/auth/register", {
                name: data.name.trim(),
                roll_number: data.roll_number.trim(),
                contact_number: data.contact_number.trim(),
                gender: data.gender,
                password: data.password
            });

            currentUser = result.user;

            this.reset();

            showApplication();

        } catch (error) {
            showMessage("registerMessage", error.message, true);
        } finally {
            button.disabled = false;
            button.textContent = "Create Account →";
        }
    }
);

// -------------------- LOGIN --------------------

document.getElementById("loginForm").addEventListener(
    "submit",
    async function(event) {
        event.preventDefault();

        const data = getFormData(this);

        if (!validRollNumber(data.roll_number)) {
            showMessage(
                "loginMessage",
                "Invalid roll number. Roll number must contain exactly 8 alphanumeric characters.",
                true
            );
            return;
        }

        const button = this.querySelector('button[type="submit"]');
        button.disabled = true;
        button.textContent = "Logging in...";

        try {
            const result = await sendRequest("/api/auth/login", {
                roll_number: data.roll_number.trim(),
                password: data.password
            });

            currentUser = result.user;

            this.reset();

            showApplication();

        } catch (error) {
            showMessage("loginMessage", error.message, true);
        } finally {
            button.disabled = false;
            button.textContent = "Login →";
        }
    }
);

// -------------------- CHECK EXISTING SESSION --------------------

async function checkSession() {
    try {
        const result = await getRequest("/api/auth/me");

        currentUser = result.user;

        showApplication();

    } catch (error) {
        showAuthentication();
    }
}

// -------------------- PROFILE --------------------

document.getElementById("profileBtn").addEventListener("click", () => {
    document.getElementById("profilePanel").classList.toggle("hidden");
});

document.getElementById("closeProfileBtn").addEventListener("click", () => {
    document.getElementById("profilePanel").classList.add("hidden");
});

// -------------------- LOGOUT --------------------

document.getElementById("logoutBtn").addEventListener(
    "click",
    async function() {
        try {
            await sendRequest("/api/auth/logout", {});
            showAuthentication();
            showAuthForm("login");

        } catch (error) {
            showMessage("profileMessage", error.message, true);
        }
    }
);

// -------------------- NAVIGATION --------------------

function showSection(section) {
    ["home", "find", "offer", "form-group", "my-rides", "my-upcoming"].forEach(id => {
        document.getElementById(id).classList.toggle(
            "hidden",
            id !== section
        );
    });

    if (section === "my-rides") loadMyRides();
    if (section === "my-upcoming") loadUpcomingRides();

    window.scrollTo(0, 0);
}

// -------------------- FIND A RIDE --------------------

document.getElementById("findForm").addEventListener(
    "submit",
    async function(event) {
        event.preventDefault();

        const data = getFormData(this);

        if (!validRoute(data.pickup_location, data.destination)) {
            showMessage(
                "findMessage",
                "Please select different valid pickup and destination locations.",
                true
            );
            return;
        }

        if (!validRollNumber(currentUser.roll_number)) {
            showMessage("findMessage", "Invalid student roll number.", true);
            return;
        }

        if (!validPhoneNumber(data.contact_number)) {
            showMessage(
                "findMessage",
                "Invalid phone number. Please enter a valid 10-digit phone number.",
                true
            );
            return;
        }

        data.student_name = currentUser.name;
        data.roll_number = currentUser.roll_number;

        data.passengers = Number(data.passengers);
        data.luggage = Number(data.luggage);

        if (!Number.isInteger(data.passengers) ||
            data.passengers < 1 || data.passengers > 6) {
            showMessage(
                "findMessage",
                "Passenger count must be between 1 and 6.",
                true
            );
            return;
        }

        if (!Number.isInteger(data.luggage) || data.luggage < 0) {
            showMessage(
                "findMessage",
                "Please enter a valid luggage count.",
                true
            );
            return;
        }

        showMessage(
            "findMessage",
            "Searching for compatible rides..."
        );

        document.getElementById("results").innerHTML = "";

        try {
            const result = await sendRequest(
                "/api/student/search",
                data
            );

            const matches = result.matches || [];

            showMessage(
                "findMessage",
                matches.length + " compatible ride(s) found."
            );

            if (matches.length === 0) {
                document.getElementById("results").textContent =
                    "No compatible rides found. Try another time or destination.";
                return;
            }

            let html = "<h2>Available Rides</h2>";

            matches.forEach(ride => {
                html += `
                    <div class="ride-card">
                        <h3>${escapeHTML(ride.student_name)}'s
                            ${escapeHTML(ride.vehicle_type)}</h3>

                        <p><b>Route:</b>
                            ${escapeHTML(ride.pickup_location)}
                            → ${escapeHTML(ride.destination)}
                        </p>

                        <p><b>Departure:</b>
                            ${escapeHTML(ride.departure_time)}
                        </p>

                        <p><b>Available seats:</b>
                            ${Number(ride.available_seats)}
                        </p>

                        <p><b>Luggage capacity:</b>
                            ${Number(ride.luggage_capacity)}
                        </p>

                        <p><b>Estimated fare share:</b>
                            ${ride.estimated_share_per_person == null
                                ? escapeHTML(
                                    ride.fare_display ||
                                    (ride.vehicle_type === "Auto"
                                        ? "Fare depends on the auto driver's quotation."
                                        : "Fare not available")
                                )
                                : "₹" + Number(ride.estimated_share_per_person).toFixed(2)}
                        </p>

                        <button type="button" class="view-profile-btn"
                            data-profile-roll="${escapeHTML(ride.roll_number || "")}">View Profile</button>
                        <button type="button" class="join-ride-btn"
                            data-ride-id="${escapeHTML(ride.ride_id)}">
                            Request to Join
                        </button>
                    </div>
                `;
            });

            const results = document.getElementById("results");
            results.innerHTML = html;

            bindProfileButtons(results);
            results.querySelectorAll(".join-ride-btn").forEach(button => {
                button.addEventListener("click", () => {
                    requestToJoin(button.dataset.rideId);
                });
            });

        } catch (error) {
            showMessage("findMessage", error.message, true);
        }
    }
);

// -------------------- REQUEST TO JOIN --------------------

async function requestToJoin(rideId) {
    const form = document.getElementById("findForm");

    const passengers = Number(
        form.querySelector("[name='passengers']").value
    );

    const luggage = Number(
        form.querySelector("[name='luggage']").value
    );

    if (!Number.isInteger(passengers) || passengers < 1 ||
        !Number.isInteger(luggage) || luggage < 0) {
        alert("Please enter valid passenger and luggage counts before requesting to join.");
        return;
    }

    try {
        await sendRequest("/api/student/join", {
            ride_id: rideId,
            passengers,
            luggage
        });

        alert("Your request has been sent to the ride owner!");

    } catch (error) {
        alert(error.message);
    }
}


// View only registered User Info fields. The server reveals contact details only after acceptance.
async function showStudentProfile(roll) {
    if (!roll) { alert("Student roll number is unavailable."); return; }
    try {
        const profile = await getRequest(`/api/student/profile/${encodeURIComponent(roll)}`);
        const lines = [`Name: ${profile.name || ""}`, `Roll number: ${profile.roll_number || ""}`, `Gender: ${profile.gender || "Not provided"}`];
        if (profile.contact_visible) lines.push(`Contact number: ${profile.contact_number || "Not provided"}`);
        else lines.push("Contact number: Visible after your request is accepted.");
        alert(lines.join("\n"));
    } catch (error) { alert(error.message); }
}

function bindProfileButtons(root) {
    root.querySelectorAll(".view-profile-btn").forEach(button => {
        if (button.dataset.profileBound) return;
        button.dataset.profileBound = "1";
        button.addEventListener("click", () => showStudentProfile(button.dataset.profileRoll));
    });
}

// -------------------- FORM A TRAVEL GROUP --------------------

function renderGroupMatches(result) {
    const container = document.getElementById("groupResults");
    const matches = result.student_matches || [];
    container.innerHTML = "";

    if (!result.request) {
        container.textContent = "Submit your travel details to find compatible students.";
        return;
    }

    const summary = document.createElement("p");
    summary.className = "group-request-summary";
    summary.textContent = `Your request: ${result.request.pickup_location} → ${result.request.destination}, ${result.request.travel_time}.`;
    container.appendChild(summary);

    const heading = document.createElement("h2");
    heading.textContent = "Students with Compatible Travel Requests";
    container.appendChild(heading);

    if (!matches.length) {
        const empty = document.createElement("p");
        empty.textContent = "No compatible student requests found yet. Try refreshing later.";
        container.appendChild(empty);
        return;
    }

    matches.forEach(student => {
        const card = document.createElement("article");
        card.className = "ride-card upcoming-ride-card";
        card.innerHTML = `
            <h3>${escapeHTML(student.student_name)}</h3>
            <p><b>Route:</b> ${escapeHTML(student.pickup_location)} → ${escapeHTML(student.destination)}</p>
            <p><b>Travel time:</b> ${escapeHTML(student.travel_time)}</p>
            <p><b>Passengers:</b> ${Number(student.passengers)}</p>
            <p><b>Luggage:</b> ${Number(student.luggage)}</p>
            <p><b>Combined group:</b> ${Number(student.combined_passengers)} passenger(s), ${Number(student.combined_luggage)} luggage unit(s)</p>
            <button type="button" class="view-profile-btn" data-profile-roll="${escapeHTML(student.roll_number || "")}">View Profile</button>
        `;
        container.appendChild(card);
    });
    bindProfileButtons(container);
}

document.getElementById("groupForm").addEventListener("submit", async function(event) {
    event.preventDefault();
    const data = getFormData(this);

    if (!validRoute(data.pickup_location, data.destination)) {
        showMessage("groupMessage", "Please select different valid pickup and destination locations.", true);
        return;
    }

    data.passengers = Number(data.passengers);
    data.luggage = Number(data.luggage);
    if (!Number.isInteger(data.passengers) || data.passengers < 1 || data.passengers > 6 ||
        !Number.isInteger(data.luggage) || data.luggage < 0) {
        showMessage("groupMessage", "Enter a valid passenger and luggage count.", true);
        return;
    }

    showMessage("groupMessage", "Submitting your travel request...");
    try {
        const result = await sendRequest("/api/student/group-requests", data);
        renderGroupMatches(result);
        showMessage("groupMessage", "Your travel request is active. Compatible students are shown below.");
    } catch (error) {
        showMessage("groupMessage", error.message, true);
    }
});

document.getElementById("refreshGroupMatches").addEventListener("click", async () => {
    showMessage("groupMessage", "Refreshing compatible student requests...");
    try {
        const result = await getRequest("/api/student/group-matches");
        renderGroupMatches(result);
        showMessage("groupMessage", "Travel group matches refreshed.");
    } catch (error) {
        showMessage("groupMessage", error.message, true);
    }
});

async function cancelCabOffer(rideId) {
    if (!rideId) return;
    if (!window.confirm("Are you sure you want to cancel this cab offer? Pending join requests will also be cancelled.")) {
        return;
    }
    try {
        const result = await sendRequest(
            `/api/student/offers/${encodeURIComponent(rideId)}/cancel`,
            {}
        );
        showMessage("myRidesMessage", result.message || "Cab offer cancelled.");
        await loadMyRides();
    } catch (error) {
        showMessage("myRidesMessage", error.message, true);
    }
}

// -------------------- OFFER A RIDE --------------------

const cabCheckbox = document.getElementById("cabBooked");
const postButton = document.getElementById("postRideBtn");

cabCheckbox.addEventListener("change", function() {
    postButton.disabled = !this.checked;
});

document.getElementById("offerForm").addEventListener(
    "submit",
    async function(event) {
        event.preventDefault();

        const data = getFormData(this);

        if (!validRoute(data.pickup_location, data.destination)) {
            showMessage(
                "offerMessage",
                "Please select different valid pickup and destination locations.",
                true
            );
            return;
        }

        if (!validPhoneNumber(data.contact_number)) {
            showMessage(
                "offerMessage",
                "Invalid phone number. Please enter a valid 10-digit phone number.",
                true
            );
            return;
        }

        data.student_name = currentUser.name;
        data.roll_number = currentUser.roll_number;

        const allowed = VEHICLE_CAPACITIES[data.vehicle_type] || [];

        const selectedCapacity = Number(data.passenger_capacity);
        const availableSeats = Number(data.available_seats);
        const luggageCapacity = Number(data.luggage_capacity);

        if (!allowed.includes(selectedCapacity)) {
            showMessage(
                "offerMessage",
                "The selected passenger capacity is not valid for this vehicle.",
                true
            );
            return;
        }

        if (!Number.isInteger(availableSeats) ||
            availableSeats < 1 ||
            availableSeats > selectedCapacity) {
            showMessage(
                "offerMessage",
                "Available seats must be between 1 and the vehicle's passenger capacity.",
                true
            );
            return;
        }

        if (!Number.isFinite(luggageCapacity) || luggageCapacity < 0) {
            showMessage(
                "offerMessage",
                "Please enter a valid luggage capacity.",
                true
            );
            return;
        }

        const fareText = String(data.total_fare || "").trim();

        if (data.vehicle_type === "Auto" && fareText === "") {
            // Preserve unknown Auto fare as null, never convert it to zero.
            data.total_fare = null;
        } else {
            const actualFare = Number(fareText);

            if (!Number.isFinite(actualFare) || actualFare <= 0) {
                showMessage(
                    "offerMessage",
                    data.vehicle_type === "Auto"
                        ? "Enter the driver's quoted fare, or leave it empty if it is not known."
                        : "Please enter the actual total cab fare.",
                    true
                );
                return;
            }

            data.total_fare = actualFare;
        }

        data.passenger_capacity = selectedCapacity;
        data.available_seats = availableSeats;
        data.luggage_capacity = luggageCapacity;

        if (!document.getElementById("cabBooked").checked) {
            showMessage(
                "offerMessage",
                "Please confirm that you have already booked this cab.",
                true
            );
            return;
        }

        if (!currentUser) {
            showMessage("offerMessage", "Please log in first.", true);
            return;
        }

        showMessage("offerMessage", "Posting your ride...");

        postButton.disabled = true;

        try {
            const result = await sendRequest(
                "/api/student/offers",
                data
            );

            const postedFareNote = result.ride.fare_status === "DRIVER_QUOTATION_REQUIRED"
                ? " Fare depends on the auto driver's quotation."
                : "";

            showMessage(
                "offerMessage",
                "Your ride has been posted successfully! Ride ID: " +
                result.ride.ride_id + "." + postedFareNote
            );

            this.reset();

            postButton.disabled = true;

            const vehicleSelect = document.getElementById("offerVehicle");
            const capacitySelect = document.getElementById("offerCapacity");
            const fareInput = document.getElementById("offerFare");

            if (vehicleSelect) {
                vehicleSelect.dispatchEvent(new Event("change"));
            }

            if (capacitySelect) {
                capacitySelect.disabled = true;
            }

            if (fareInput) {
                fareInput.required = true;
                fareInput.value = "";
            }

            fillStudentDetails();

        } catch (error) {
            showMessage("offerMessage", error.message, true);
            postButton.disabled = !cabCheckbox.checked;
        }
    }
);

// -------------------- MY RIDES --------------------

async function loadMyRides() {
    try {
        const data = await getRequest("/api/my-rides");

        const groupBox = document.getElementById("myGroupRequests");
        const offersBox = document.getElementById("myOfferedRides");
        const incomingBox = document.getElementById("incomingJoinRequests");
        const sentBox = document.getElementById("mySentJoinRequests");

        groupBox.innerHTML = "";
        offersBox.innerHTML = "";
        incomingBox.innerHTML = "";
        sentBox.innerHTML = "";

        (data.group_requests || []).forEach(r => {
            const el = document.createElement("p");

            el.textContent =
                `${r.request_id}: ${r.pickup_location} → ${r.destination}, ${r.earliest_departure} — submitted`;

            groupBox.appendChild(el);
        });

        (data.offered_rides || []).forEach(r => {
            const el = document.createElement("article");
            el.className = "ride-card upcoming-ride-card";

            const fare = formatFare(
                r.total_fare,
                r.fare_status || (r.total_fare == null ? "DRIVER_QUOTATION_REQUIRED" : "ACTUAL")
            );

            el.innerHTML = `
                <h3>${escapeHTML(r.vehicle_type)} — ${escapeHTML(r.ride_id)}</h3>
                <p>${escapeHTML(r.pickup_location)} → ${escapeHTML(r.destination)}</p>
                <p>Departure: ${escapeHTML(r.departure_time)}</p>
                <p>Total fare: ${fare}</p>
                <p>Available seats: ${Number(r.available_seats)}</p>
                <p>Available luggage: ${Number(r.luggage_capacity)}</p>
                <p class="accepted-passengers-label">Accepted Passengers: ${(r.accepted_passengers || []).length}</p>
                <p><b>Status:</b> ${escapeHTML(r.status || "OPEN")}</p>
                ${r.status === "OPEN" ? `
                    <button type="button" class="cancel-offer-btn"
                        data-ride-id="${escapeHTML(r.ride_id)}">Cancel Offer</button>
                ` : ""}
            `;

            const cancelButton = el.querySelector(".cancel-offer-btn");
            if (cancelButton) {
                cancelButton.addEventListener("click", () => {
                    cancelCabOffer(r.ride_id);
                });
            }

            offersBox.appendChild(el);
        });

        (data.incoming_requests || []).forEach(j => {
            if (j.status !== "PENDING") return;

            const el = document.createElement("article");
            el.className = "ride-card upcoming-ride-card";

            el.innerHTML = `
                <p>${escapeHTML(j.request_name)}
                (${escapeHTML(j.request_roll_number)}) —
                ${Number(j.passengers)} passenger(s),
                ${Number(j.luggage)} luggage unit(s)</p>
                <p>Status: PENDING</p>
                <button type="button" class="view-profile-btn" data-profile-roll="${escapeHTML(j.request_roll_number || "")}">View Profile</button>
                <button type="button" data-action="accept">Accept Request</button>
                <button type="button" data-action="reject">Reject Request</button>
            `;

            el.querySelectorAll("button").forEach(btn => {
                btn.addEventListener("click", () => {
                    processJoinRequest(j.join_id, btn.dataset.action);
                });
            });

            incomingBox.appendChild(el);
        });
        bindProfileButtons(incomingBox);

        (data.sent_join_requests || []).forEach(j => {
            const el = document.createElement("p");
            el.textContent = `${j.ride_id}: ${j.status}${j.cancellation_reason ? " — " + j.cancellation_reason : ""}`;
            sentBox.appendChild(el);
        });

        if (!groupBox.children.length) {
            groupBox.textContent = "No group requests yet.";
        }

        if (!offersBox.children.length) {
            offersBox.textContent = "No cab offers yet.";
        }

        if (!incomingBox.children.length) {
            incomingBox.textContent = "No incoming join requests.";
        }

        if (!sentBox.children.length) {
            sentBox.textContent = "No join requests sent.";
        }

        showMessage("myRidesMessage", "Your ride activity is up to date.");

    } catch (error) {
        showMessage("myRidesMessage", error.message, true);
    }
}

async function processJoinRequest(joinId, action) {
    try {
        await sendRequest(
            `/api/student/join-requests/${encodeURIComponent(joinId)}/${action}`,
            {}
        );

        showMessage(
            "myRidesMessage",
            action === "accept"
                ? "Join request accepted."
                : "Join request rejected."
        );

        await loadMyRides();

    } catch (error) {
        showMessage("myRidesMessage", error.message, true);
    }
}

// -------------------- MY UPCOMING RIDE --------------------

async function loadUpcomingRides() {
    const container = document.getElementById("upcomingRides");
    const history = document.getElementById("upcomingHistory");

    container.innerHTML = "";
    history.innerHTML = "";

    showMessage("upcomingMessage", "Loading your confirmed rides...");

    try {
        const result = await getRequest("/api/my-upcoming");

        const rides = result.rides || [];

        showMessage(
            "upcomingMessage",
            rides.length
                ? `${rides.length} confirmed upcoming ride(s).`
                : "You do not have any confirmed upcoming rides."
        );

        rides.forEach(ride => {
            const departure = escapeHTML(
                ride.departure_time || "Not available"
            );

            const fare = isUnknownFare(ride.total_fare)
                ? escapeHTML(
                    ride.fare_display ||
                    (ride.vehicle_type === "Auto"
                        ? "Fare depends on the auto driver's quotation."
                        : "Fare not yet known")
                )
                : formatFare(ride.total_fare, ride.fare_status || "fare");

            const share = formatFareShare(
                ride.individual_fare_share,
                ride.vehicle_type
            );

            const people = (ride.other_students || []).map(student =>
                `<li>${escapeHTML(student.name)}
                (${escapeHTML(student.roll_number)}) —
                ${Number(student.passengers || 1)} passenger(s)
                <button type="button" class="view-profile-btn" data-profile-roll="${escapeHTML(student.roll_number || "")}">View Profile</button></li>`
            ).join("") || "<li>Passenger details are not available.</li>";

            const card = document.createElement("article");
            card.className = "ride-card upcoming-ride-card";

            card.innerHTML = `
                <h2>${escapeHTML(ride.pickup_location)}
                    → ${escapeHTML(ride.destination)}</h2>

                <p><b>Travel date & departure:</b> ${departure}</p>

                <p><b>Vehicle:</b>
                    ${escapeHTML(ride.vehicle_type)}</p>

                <p><b>Total vehicle fare:</b> ${fare}</p>

                <p><b>Your fare share:</b> ${share}</p>

                <p><b>Passenger count:</b>
                    ${Number(ride.passengers || 1)}</p>

                <p><b>Available seats:</b>
                    ${ride.available_seats == null
                        ? "Not applicable"
                        : Number(ride.available_seats)}</p>

                <h3>Other students in this ride</h3>
                <ul>${people}</ul>

                <button type="button"
                    class="cancel-participation-btn"
                    data-participation-id="${escapeHTML(ride.participation_id)}">
                    Cancel My Participation
                </button>
            `;

            card.querySelector("button").addEventListener("click", () => {
                cancelMyParticipation(ride.participation_id);
            });

            container.appendChild(card);
            bindProfileButtons(card);
        });

        (result.history || []).forEach(item => {
            const row = document.createElement("p");
            row.textContent = `${item.ride_id} — CANCELLED`;
            history.appendChild(row);
        });

        if (!history.children.length) {
            history.textContent = "No cancelled rides in your history.";
        }

    } catch (error) {
        showMessage("upcomingMessage", error.message, true);
    }
}

// -------------------- CANCEL MY PARTICIPATION --------------------

async function cancelMyParticipation(participationId) {
    if (!participationId) {
        showMessage(
            "upcomingMessage",
            "Unable to identify this ride participation.",
            true
        );
        return;
    }

    if (!window.confirm(
        "Are you sure you want to cancel your participation in this ride?"
    )) {
        return;
    }

    try {
        const result = await sendRequest(
            `/api/my-upcoming/${encodeURIComponent(participationId)}/cancel`,
            {}
        );

        showMessage(
            "upcomingMessage",
            result.message || "Your participation has been cancelled successfully."
        );

        await loadUpcomingRides();

        // Refresh My Rides data as well.
        await loadMyRides();

    } catch (error) {
        showMessage("upcomingMessage", error.message, true);
    }
}

// -------------------- START APPLICATION --------------------

document.addEventListener("DOMContentLoaded", checkSession);