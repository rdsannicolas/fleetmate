import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import App from './App';

// Smoke test: proves the Vitest + React Testing Library harness renders a
// component and queries the DOM. Feature behaviour is tested in later tasks.
describe('App shell', () => {
  it('renders the application title', () => {
    render(<App />);
    expect(screen.getByRole('heading', { name: /fleetmate/i })).toBeInTheDocument();
  });
});
