DROP TABLE IF EXISTS tickets;
DROP TABLE IF EXISTS agents;
DROP TABLE IF EXISTS teams;
DROP TABLE IF EXISTS priorities;
DROP TABLE IF EXISTS ticket_statuses;

CREATE TABLE teams (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(80) NOT NULL UNIQUE
) ENGINE=InnoDB;

CREATE TABLE agents (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(100) NOT NULL,
  team_id INT NOT NULL,
  FOREIGN KEY (team_id) REFERENCES teams(id)
) ENGINE=InnoDB;

CREATE TABLE ticket_statuses (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(40) NOT NULL UNIQUE,
  is_open TINYINT(1) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE priorities (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(20) NOT NULL UNIQUE,
  rank_order TINYINT NOT NULL
) ENGINE=InnoDB;

CREATE TABLE tickets (
  id INT AUTO_INCREMENT PRIMARY KEY,
  subject VARCHAR(200) NOT NULL,
  description TEXT,
  requester_name VARCHAR(100) NOT NULL,
  requester_email VARCHAR(160) NOT NULL,
  status_id INT NOT NULL,
  priority_id INT NOT NULL,
  team_id INT NOT NULL,
  assignee_id INT NULL,
  created_at DATETIME NOT NULL,
  resolved_at DATETIME NULL,
  INDEX idx_tickets_created (created_at),
  FOREIGN KEY (status_id) REFERENCES ticket_statuses(id),
  FOREIGN KEY (priority_id) REFERENCES priorities(id),
  FOREIGN KEY (team_id) REFERENCES teams(id),
  FOREIGN KEY (assignee_id) REFERENCES agents(id)
) ENGINE=InnoDB;
