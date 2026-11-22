CREATE TABLE departments (
	id UUID NOT NULL, 
	code VARCHAR(64) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (code)
);

CREATE TABLE terms (
	id UUID NOT NULL, 
	code VARCHAR(64) NOT NULL, 
	start_date DATE NOT NULL, 
	end_date DATE NOT NULL, 
	catalog_published_at TIMESTAMP WITH TIME ZONE, 
	allocation_seed TEXT NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_term_dates CHECK (start_date <= end_date), 
	UNIQUE (code)
);

CREATE TABLE users (
	id UUID NOT NULL, 
	login VARCHAR(255) NOT NULL, 
	email VARCHAR(320) NOT NULL, 
	password_hash TEXT NOT NULL, 
	role VARCHAR(16) NOT NULL, 
	is_active BOOLEAN DEFAULT true NOT NULL, 
	display_name VARCHAR(255) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_login_normalized CHECK (login = lower(btrim(login)) AND login <> ''), 
	CONSTRAINT ck_user_role CHECK (role IN ('STUDENT','ADMIN')), 
	UNIQUE (login)
);

CREATE TABLE allocation_runs (
	id UUID NOT NULL, 
	term_id UUID NOT NULL, 
	algorithm_version VARCHAR(64) NOT NULL, 
	seed TEXT NOT NULL, 
	published_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	accepted_count INTEGER DEFAULT '0' NOT NULL, 
	rejected_count INTEGER DEFAULT '0' NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_run_term UNIQUE (term_id, id), 
	CONSTRAINT ck_run_counts CHECK (accepted_count >= 0 AND rejected_count >= 0), 
	UNIQUE (term_id), 
	FOREIGN KEY(term_id) REFERENCES terms (id) ON DELETE RESTRICT
);

CREATE TABLE audit_events (
	id UUID NOT NULL, 
	actor_user_id UUID, 
	term_id UUID NOT NULL, 
	action VARCHAR(128) NOT NULL, 
	target_type VARCHAR(64) NOT NULL, 
	target_id UUID, 
	occurred_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	payload JSONB DEFAULT '{}'::jsonb NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(actor_user_id) REFERENCES users (id) ON DELETE RESTRICT, 
	FOREIGN KEY(term_id) REFERENCES terms (id) ON DELETE RESTRICT
);

CREATE TABLE auth_sessions (
	token_hash VARCHAR(64) NOT NULL, 
	user_id UUID NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	revoked_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (token_hash), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE TABLE courses (
	id UUID NOT NULL, 
	code VARCHAR(64) NOT NULL, 
	department_id UUID NOT NULL, 
	title VARCHAR(255) NOT NULL, 
	description TEXT DEFAULT '' NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (code), 
	FOREIGN KEY(department_id) REFERENCES departments (id) ON DELETE RESTRICT
);

CREATE TABLE instructors (
	id UUID NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	department_id UUID NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(department_id) REFERENCES departments (id) ON DELETE RESTRICT
);

CREATE TABLE notification_outbox (
	id UUID NOT NULL, 
	event_key VARCHAR(255) NOT NULL, 
	term_id UUID NOT NULL, 
	recipient_user_id UUID NOT NULL, 
	type VARCHAR(64) NOT NULL, 
	payload JSONB NOT NULL, 
	status VARCHAR(16) DEFAULT 'PENDING' NOT NULL, 
	attempt_count INTEGER DEFAULT '0' NOT NULL, 
	next_attempt_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	lease_until TIMESTAMP WITH TIME ZONE, 
	claim_token UUID, 
	sent_at TIMESTAMP WITH TIME ZONE, 
	last_error TEXT, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_outbox_status CHECK (status IN ('PENDING','PROCESSING','SENT','FAILED')), 
	CONSTRAINT ck_outbox_attempts CHECK (attempt_count BETWEEN 0 AND 3), 
	UNIQUE (event_key), 
	FOREIGN KEY(term_id) REFERENCES terms (id) ON DELETE RESTRICT, 
	FOREIGN KEY(recipient_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE TABLE registration_periods (
	term_id UUID NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	starts_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	ends_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (term_id, kind), 
	CONSTRAINT ck_period_kind CHECK (kind IN ('INITIAL','ADD_DROP')), 
	CONSTRAINT ck_period_order CHECK (starts_at < ends_at), 
	FOREIGN KEY(term_id) REFERENCES terms (id) ON DELETE RESTRICT
);

CREATE TABLE students (
	id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	student_no VARCHAR(64) NOT NULL, 
	department_id UUID NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (user_id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT, 
	UNIQUE (student_no), 
	FOREIGN KEY(department_id) REFERENCES departments (id) ON DELETE RESTRICT
);

CREATE TABLE course_completions (
	student_id UUID NOT NULL, 
	course_id UUID NOT NULL, 
	completed_term_id UUID NOT NULL, 
	PRIMARY KEY (student_id, course_id, completed_term_id), 
	FOREIGN KEY(student_id) REFERENCES students (id) ON DELETE RESTRICT, 
	FOREIGN KEY(course_id) REFERENCES courses (id) ON DELETE RESTRICT, 
	FOREIGN KEY(completed_term_id) REFERENCES terms (id) ON DELETE RESTRICT
);

CREATE TABLE sections (
	id UUID NOT NULL, 
	term_id UUID NOT NULL, 
	course_id UUID NOT NULL, 
	section_no VARCHAR(32) NOT NULL, 
	instructor_id UUID NOT NULL, 
	credits NUMERIC(4, 1) NOT NULL, 
	capacity INTEGER NOT NULL, 
	is_selectable BOOLEAN DEFAULT true NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_section_number UNIQUE (term_id, course_id, section_no), 
	CONSTRAINT uq_section_identity UNIQUE (id, term_id, course_id), 
	CONSTRAINT ck_section_credits CHECK (credits > 0), 
	CONSTRAINT ck_section_capacity CHECK (capacity >= 0), 
	FOREIGN KEY(term_id) REFERENCES terms (id) ON DELETE RESTRICT, 
	FOREIGN KEY(course_id) REFERENCES courses (id) ON DELETE RESTRICT, 
	FOREIGN KEY(instructor_id) REFERENCES instructors (id) ON DELETE RESTRICT
);

CREATE TABLE student_terms (
	student_id UUID NOT NULL, 
	term_id UUID NOT NULL, 
	credit_limit NUMERIC(4, 1) NOT NULL, 
	PRIMARY KEY (student_id, term_id), 
	CONSTRAINT ck_credit_limit CHECK (credit_limit >= 0), 
	FOREIGN KEY(student_id) REFERENCES students (id) ON DELETE RESTRICT, 
	FOREIGN KEY(term_id) REFERENCES terms (id) ON DELETE RESTRICT
);

CREATE TABLE applications (
	id UUID NOT NULL, 
	student_id UUID NOT NULL, 
	term_id UUID NOT NULL, 
	course_id UUID NOT NULL, 
	section_id UUID NOT NULL, 
	status VARCHAR(16) DEFAULT 'PENDING' NOT NULL, 
	submitted_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	decided_at TIMESTAMP WITH TIME ZONE, 
	reason_code VARCHAR(64), 
	allocation_run_id UUID, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_application_student_term FOREIGN KEY(student_id, term_id) REFERENCES student_terms (student_id, term_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_application_section FOREIGN KEY(section_id, term_id, course_id) REFERENCES sections (id, term_id, course_id) ON DELETE RESTRICT, 
	CONSTRAINT uq_application_section UNIQUE (student_id, term_id, section_id), 
	CONSTRAINT fk_application_run FOREIGN KEY(term_id, allocation_run_id) REFERENCES allocation_runs (term_id, id) ON DELETE RESTRICT, 
	CONSTRAINT ck_application_status CHECK (status IN ('PENDING','CANCELLED','ACCEPTED','REJECTED'))
);

CREATE TABLE enrollments (
	id UUID NOT NULL, 
	student_id UUID NOT NULL, 
	term_id UUID NOT NULL, 
	course_id UUID NOT NULL, 
	section_id UUID NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	source VARCHAR(16) NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_enrollment_student_term FOREIGN KEY(student_id, term_id) REFERENCES student_terms (student_id, term_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_enrollment_section FOREIGN KEY(section_id, term_id, course_id) REFERENCES sections (id, term_id, course_id) ON DELETE RESTRICT, 
	CONSTRAINT uq_enrollment_section UNIQUE (student_id, term_id, section_id), 
	CONSTRAINT ck_enrollment_status CHECK (status IN ('ENROLLED','DROPPED')), 
	CONSTRAINT ck_enrollment_source CHECK (source IN ('INITIAL','ADD_DROP'))
);

CREATE TABLE idempotency_keys (
	student_id UUID NOT NULL, 
	term_id UUID NOT NULL, 
	request_key VARCHAR(200) NOT NULL, 
	request_fingerprint VARCHAR(64) NOT NULL, 
	response_status INTEGER NOT NULL, 
	response_body JSONB NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (student_id, term_id, request_key), 
	FOREIGN KEY(student_id, term_id) REFERENCES student_terms (student_id, term_id) ON DELETE RESTRICT, 
	CONSTRAINT ck_idempotency_success CHECK (response_status IN (201,204))
);

CREATE TABLE section_meetings (
	id UUID NOT NULL, 
	section_id UUID NOT NULL, 
	weekday INTEGER NOT NULL, 
	start_time TIME WITHOUT TIME ZONE NOT NULL, 
	end_time TIME WITHOUT TIME ZONE NOT NULL, 
	room VARCHAR(255) DEFAULT '' NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_meeting UNIQUE (section_id, weekday, start_time, end_time), 
	CONSTRAINT ck_meeting_day CHECK (weekday BETWEEN 1 AND 7), 
	CONSTRAINT ck_meeting_time CHECK (start_time < end_time), 
	FOREIGN KEY(section_id) REFERENCES sections (id) ON DELETE RESTRICT
);

CREATE TABLE section_prerequisites (
	section_id UUID NOT NULL, 
	required_course_id UUID NOT NULL, 
	PRIMARY KEY (section_id, required_course_id), 
	FOREIGN KEY(section_id) REFERENCES sections (id) ON DELETE RESTRICT, 
	FOREIGN KEY(required_course_id) REFERENCES courses (id) ON DELETE RESTRICT
);

CREATE INDEX ix_auth_sessions_user_id ON auth_sessions (user_id);

CREATE INDEX ix_outbox_poll ON notification_outbox (status, next_attempt_at);

CREATE INDEX ix_sections_term ON sections (term_id);

CREATE INDEX ix_pending_term ON applications (term_id) WHERE status = 'PENDING';

CREATE UNIQUE INDEX uq_pending_course ON applications (student_id, term_id, course_id) WHERE status = 'PENDING';

CREATE INDEX ix_enrolled_section ON enrollments (section_id) WHERE status = 'ENROLLED';

CREATE UNIQUE INDEX uq_enrolled_course ON enrollments (student_id, term_id, course_id) WHERE status = 'ENROLLED';

CREATE VIEW v_section_availability AS
SELECT s.id AS section_id, s.term_id, s.course_id, s.capacity,
       COALESCE(e.n,0)::integer AS enrolled_count,
       (s.capacity-COALESCE(e.n,0))::integer AS remaining
FROM sections s JOIN terms t ON t.id=s.term_id
LEFT JOIN (SELECT section_id, COUNT(*) AS n FROM enrollments WHERE status='ENROLLED' GROUP BY section_id) e ON e.section_id=s.id
WHERE t.catalog_published_at IS NOT NULL;

CREATE VIEW v_public_section_catalog AS
SELECT s.id AS section_id, s.term_id, t.code AS term_code, s.course_id,
       c.code, c.title, c.description, c.department_id, d.name AS department_name,
       s.section_no, s.instructor_id, i.name AS instructor_name, s.credits,
       s.capacity, s.is_selectable, a.enrolled_count, a.remaining
FROM sections s
JOIN terms t ON t.id=s.term_id
JOIN courses c ON c.id=s.course_id
JOIN departments d ON d.id=c.department_id
JOIN instructors i ON i.id=s.instructor_id
JOIN v_section_availability a ON a.section_id=s.id
WHERE t.catalog_published_at IS NOT NULL;

CREATE VIEW v_public_section_meetings AS
SELECT m.id, m.section_id, s.term_id, m.weekday, m.start_time, m.end_time, m.room
FROM section_meetings m JOIN sections s ON s.id=m.section_id
JOIN terms t ON t.id=s.term_id WHERE t.catalog_published_at IS NOT NULL;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO course_app, course_public;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO course_app;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM course_public;
GRANT SELECT ON v_section_availability,v_public_section_catalog,v_public_section_meetings TO course_public;
ALTER ROLE course_public SET default_transaction_read_only=on;
ALTER ROLE course_public SET statement_timeout='2s';
