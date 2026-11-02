
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

    const fields = [
        ["findName", name],
        ["findRoll", roll],
        ["offerName", name],
        ["offerRoll", roll]
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
        currentUser.contact_number || "Not added";

    document.getElementById("profileGender").textContent =
        currentUser.gender || "Not added";

    const initial = name.trim().charAt(0).toUpperCase() || "?";

    document.getElementById("profileInitial").textContent = initial;
    document.getElementById("profileAvatar").textContent = initial;
}

function showApplication() {
    document.getElementById("authSection").classList.add("hidden");
    document.getElementById("appMain").classList.remove("hidden");
    document.getElementById("profileBtn").classList.remove("hidden");

    fillStudentDetails();
    showSection("home");
}

function showAuthentication() {
    document.getElementById("authSection").classList.remove("hidden");
    document.getElementById("appMain").classList.add("hidden");
    document.getElementById("profileBtn").classList.add("hidden");
    document.getElementById("profilePanel").classList.add("hidden");

    currentUser = null;
}

// -------------------- REGISTER --------------------

document.getElementById("registerForm").addEventListener(
    "submit",
    async function(event) {
        event.preventDefault();

        const data = getFormData(this);

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
    ["home", "find", "offer"].forEach(id => {
        document.getElementById(id).classList.toggle(
            "hidden",
            id !== section
        );
    });

    window.scrollTo(0, 0);
}

// -------------------- FIND A RIDE --------------------

document.getElementById("findForm").addEventListener(
    "submit",
    async function(event) {
        event.preventDefault();

        const data = getFormData(this);

        data.student_name = currentUser.name;
        data.roll_number = currentUser.roll_number;

        data.passengers = Number(data.passengers);
        data.luggage = Number(data.luggage);

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

            showMessage(
                "findMessage",
                result.matches.length + " compatible ride(s) found."
            );

            if (result.matches.length === 0) {
                document.getElementById("results").textContent =
                    "No compatible rides found. Try another time or destination.";
                return;
            }

            let html = "<h2>Available Rides</h2>";

            result.matches.forEach(ride => {
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

                        <p><b>Estimated fare per person:</b>
                            ₹${Number(ride.estimated_share_per_person)}
                        </p>

                        <button type="button" class="join-ride-btn"
                            data-ride-id="${escapeHTML(ride.ride_id)}">
                            Request to Join
                        </button>
                    </div>
                `;
            });

            const results = document.getElementById("results");
            results.innerHTML = html;

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

    try {
        await sendRequest("/api/student/join", {
            ride_id: rideId,
            request_roll_number: currentUser.roll_number,
            request_name: currentUser.name,
            request_contact: form.querySelector(
                "[name='contact_number']"
            ).value,
            passengers,
            luggage
        });

        alert("Your request has been sent to the ride owner!");

    } catch (error) {
        alert(error.message);
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

        data.student_name = currentUser.name;
        data.roll_number = currentUser.roll_number;

        data.passenger_capacity = Number(data.passenger_capacity);
        data.available_seats = Number(data.available_seats);
        data.luggage_capacity = Number(data.luggage_capacity);
        data.total_fare = Number(data.total_fare);

        if (data.available_seats > data.passenger_capacity) {
            showMessage(
                "offerMessage",
                "Available seats cannot exceed total passenger capacity.",
                true
            );
            return;
        }

        showMessage("offerMessage", "Posting your ride...");

        postButton.disabled = true;

        try {
            const result = await sendRequest(
                "/api/student/offers",
                data
            );

            showMessage(
                "offerMessage",
                "Your ride has been posted successfully! Ride ID: " +
                result.ride.ride_id
            );

            this.reset();

            postButton.disabled = true;

            fillStudentDetails();

        } catch (error) {
            showMessage("offerMessage", error.message, true);
            postButton.disabled = !cabCheckbox.checked;
        }
    }
);

// -------------------- START APPLICATION --------------------

document.addEventListener("DOMContentLoaded", checkSession);
